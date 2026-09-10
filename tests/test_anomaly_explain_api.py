# -*- coding: utf-8 -*-
"""
LLM 설명 계층(POST /api/anomalies/explain) 검증.

**외부 LLM API를 실제로 호출하지 않는다.** 호출 결과가 매번 달라 회귀 테스트로 쓸 수
없고(실측에서 같은 모델이 2.8초 -> 60초 타임아웃으로 급변한 적도 있다), 여기서 확인해야
할 것은 모델의 문장력이 아니라 **우리 코드의 계약**이기 때문이다:

  - 환각 검증기가 지어낸 숫자를 잡아내는가 / 추적 가능한 값을 오탐하지 않는가
  - 라이브 호출이 실패했을 때 캐시로 넘어가는가(source='cache')
  - 없는 이벤트에 404, 키가 없을 때 503을 주는가
  - 수치를 클라이언트 입력이 아니라 DB에서 읽는가

실제 모델 품질 비교는 docs/LLM_모델선정_비교실험.md, 프로덕션 실호출 결과는
docs/LLM_설명API_검증결과.md 참고.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from ami_db.narrate import (
    NarrationResult,
    build_allowed_numbers,
    build_prompt_payload,
    verify_numbers,
)

# 실제 DB의 위험 데모 이벤트(A-L-60 충북식당)와 같은 값
DANGER_EVENT = {
    "store_id": 12,
    "store_name": "충북식당",
    "biz_category_mid": "한식",
    "contract_power_kw": 45.0,
    "level": "위험",
    "rule_triggered": "kec212_overload_130pct_60min",
    "detected_at": datetime(2026, 7, 2, 2, 15),
    "metric_value": 18.94857157639569,
    "threshold_value": 14.625,
}

DANGER_STORE_ID = 12
DANGER_TS = "2026-07-02T02:15:00"
CAUTION_STORE_ID = 15
CAUTION_TS = "2026-07-22T05:45:00"


# ============================================================
# 환각 검증기 - 이 계층을 채택한 근거이므로 가장 촘촘히 본다
# ============================================================

@pytest.fixture(scope="module")
def allowed():
    return build_allowed_numbers(DANGER_EVENT)


class TestVerifyNumbers:
    @pytest.mark.parametrize("text", [
        "관측 18.95kWh, 임계 14.62kWh, 계약전력 45kW",   # 입력값 그대로
        "KEC 212.3 기준 계약전력 130%가 60분 지속",       # 규칙이 명시한 수치
        "임계치 14.62kWh는 순간전력 58.5kW에 해당합니다",  # 15분kWh -> kW 환산(x4)
        "관측값을 환산하면 75.79kW입니다",                # 같은 환산
        "임계보다 4.32kWh 높습니다",                      # 관측-임계 차이
        "2026-07-02 02:15에 감지되었습니다",              # 감지 시각 구성요소
    ])
    def test_traceable_numbers_pass(self, allowed, text):
        """입력에서 추적 가능한 값(인용·단위환산·차이)은 오탐하지 않아야 한다."""
        assert verify_numbers(text, allowed) == []

    @pytest.mark.parametrize("text,expected", [
        ("내부 온도가 78도까지 상승했습니다", ["78"]),
        ("관측 25.7kWh를 기록했습니다", ["25.7"]),
        ("예상 피해액 350만원", ["350"]),
        # 대처방안이 생기면서 모델이 금액·기간을 지어낼 자리가 늘었다 - 특히 이쪽을 본다.
        ("초과요금 47만원이 부과됩니다", ["47"]),
        ("지난 5일간 9회 발생했습니다", ["5", "9"]),
    ])
    def test_fabricated_numbers_detected(self, allowed, text, expected):
        """입력 어디에도 없는 숫자는 반드시 잡아야 한다(이게 이 기능의 존재 이유)."""
        assert verify_numbers(text, allowed) == expected

    @pytest.mark.parametrize("text", [
        "한전 고객센터 123으로 문의하세요",
        "24시간 매장은 720시간 특례를 신청할 수 있습니다",
        "계약전력 20kW 이상이면 15분 단위로 확인합니다",
    ])
    def test_guidance_numbers_pass(self, allowed, text):
        """
        대처방안 근거로 프롬프트에 넣어준 참고 안내사항(OWNER_GUIDANCE)의 수치는
        입력의 일부이므로 통과해야 한다. 이게 막히면 대처방안 문장이 통째로
        '확인 필요'로 찍혀 검증 배지가 무의미해진다.
        """
        assert verify_numbers(text, allowed) == []

    def test_rounding_tolerance(self, allowed):
        """18.94857...을 18.95로 반올림해 써도 통과해야 한다(표기 흔들림 허용)."""
        assert verify_numbers("관측값은 18.95kWh입니다", allowed) == []
        # 다만 허용 오차(0.5%)를 벗어나면 잡힌다
        assert verify_numbers("관측값은 19.5kWh입니다", allowed) == ["19.5"]


class TestPromptPayload:
    def test_datetime_and_decimal_are_serializable(self):
        """DB에서 오는 datetime/Decimal이 그대로 들어가면 JSON 직렬화가 깨진다(실제 발생한 버그)."""
        import json

        payload = build_prompt_payload(DANGER_EVENT)
        json.dumps(payload, ensure_ascii=False)  # 예외가 나면 실패
        assert payload["감지시각"] == "2026-07-02 02:15"

    def test_long_float_is_rounded(self):
        """18.94857157639569를 그대로 주면 모델이 그 긴 숫자를 문장에 옮겨 적는다."""
        assert build_prompt_payload(DANGER_EVENT)["관측_전력량_kWh_15분"] == 18.95

    def test_rule_code_is_translated(self):
        """규칙 코드값이 아니라 근거 조문이 담긴 문장이 모델에 전달돼야 한다."""
        assert "KEC 212.3" in build_prompt_payload(DANGER_EVENT)["발동규칙"]

    def test_guidance_is_included(self):
        """
        대처방안·문의초안의 근거(한전 증설 제도 등)가 프롬프트에 실려야 한다.
        빠지면 모델이 제도를 스스로 지어내게 되고, 서술형 환각은 verify_numbers가
        잡지 못한다.
        """
        guidance = build_prompt_payload(DANGER_EVENT)["참고_안내사항"]
        assert "증설" in guidance and "123" in guidance


# ============================================================
# 엔드포인트 - LLM은 monkeypatch로 대체
# ============================================================

def _fake_result(**overrides) -> NarrationResult:
    base = {
        "owner_sms": "충북식당 점주님, 관측 18.95kWh가 임계 14.62kWh를 초과했습니다. 점검 부탁드립니다.",
        "admin_note": "KEC 212.3 기준 계약전력 130%가 60분 지속.",
        "emergency_report": None,
        "model": "test-model",
        "elapsed_ms": 10,
        "verification_passed": True,
        "unknown_numbers": [],
    }
    base.update(overrides)
    return NarrationResult(**base)


class TestPreferCache:
    """prefer_cache=true면 LLM을 아예 호출하지 않고 캐시본을 즉시 돌려준다(시연용 옵션)."""

    def test_cache_hit_skips_llm_entirely(self, monkeypatch, tmp_path):
        import ami_db.narrate as narrate

        called = {"n": 0}

        def _should_not_be_called(*args, **kwargs):
            called["n"] += 1
            raise AssertionError("prefer_cache=True인데 LLM을 호출했다")

        cache_file = tmp_path / "narration_cache.json"
        cache_file.write_text(
            '{"12:2026-07-02 02:15": {"owner_sms":"캐시된 문자","admin_note":"캐시된 사유",'
            '"emergency_report":null,"model":"cached-model","elapsed_ms":1234,'
            '"verification_passed":true,"unknown_numbers":[]}}',
            encoding="utf-8",
        )
        monkeypatch.setattr(narrate, "CACHE_PATH", cache_file)
        monkeypatch.setattr(narrate, "_call_llm", _should_not_be_called)

        result = narrate.narrate_event(DANGER_EVENT, prefer_cache=True)
        assert result.source == "cache"
        assert result.owner_sms == "캐시된 문자"
        assert called["n"] == 0

    def test_cache_miss_falls_through_to_llm(self, monkeypatch, tmp_path):
        """캐시에 없으면 prefer_cache여도 평소대로 LLM을 시도해야 한다."""
        import ami_db.narrate as narrate

        monkeypatch.setattr(narrate, "CACHE_PATH", tmp_path / "empty.json")
        monkeypatch.setattr(
            narrate, "_call_llm",
            lambda event, model, timeout: ('{"owner_sms":"새로 생성","admin_note":"새 사유"}', 50),
        )
        result = narrate.narrate_event(DANGER_EVENT, prefer_cache=True)
        assert result.source == "live"
        assert result.owner_sms == "새로 생성"


class TestExplainEndpoint:
    def test_prefer_cache_is_passed_through(self, client, monkeypatch):
        """body의 prefer_cache가 narrate_event까지 전달돼야 한다."""
        seen = {}

        def _capture(event, **kw):
            seen.update(kw)
            return _fake_result(source="cache")

        monkeypatch.setattr("app.serving_api.narrate_event", _capture)
        client.post(
            "/api/anomalies/explain",
            json={"store_id": DANGER_STORE_ID, "detected_at": DANGER_TS, "prefer_cache": True},
        )
        assert seen.get("prefer_cache") is True

    def test_normal_returns_three_drafts(self, client, monkeypatch):
        """정상: 규칙이 확정한 등급/규칙을 그대로 싣고 문장 3종을 돌려준다."""
        monkeypatch.setattr(
            "app.serving_api.narrate_event",
            lambda event, **kw: _fake_result(emergency_report="긴급 점검이 필요합니다."),
        )
        resp = client.post(
            "/api/anomalies/explain",
            json={"store_id": DANGER_STORE_ID, "detected_at": DANGER_TS},
        )
        assert resp.status_code == 200
        body = resp.json()
        # 등급과 규칙은 LLM이 아니라 DB(규칙 판정 결과)에서 온다
        assert body["level"] == "위험"
        assert body["rule_triggered"] == "kec212_overload_130pct_60min"
        assert body["store_name"] == "충북식당"
        assert body["owner_sms"] and body["admin_note"]
        assert body["verification_passed"] is True
        assert body["source"] == "live"

    def test_caution_event_has_no_emergency_report(self, client, monkeypatch):
        """주의 등급이면 신고 초안은 null이어야 한다."""
        monkeypatch.setattr("app.serving_api.narrate_event", lambda event, **kw: _fake_result())
        resp = client.post(
            "/api/anomalies/explain",
            json={"store_id": CAUTION_STORE_ID, "detected_at": CAUTION_TS},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["level"] == "주의"
        assert body["emergency_report"] is None

    def test_verification_failure_is_surfaced_not_hidden(self, client, monkeypatch):
        """검증 실패를 감추지 않고 그대로 노출해야 사람이 판단할 수 있다."""
        monkeypatch.setattr(
            "app.serving_api.narrate_event",
            lambda event, **kw: _fake_result(verification_passed=False, unknown_numbers=["78"]),
        )
        resp = client.post(
            "/api/anomalies/explain",
            json={"store_id": DANGER_STORE_ID, "detected_at": DANGER_TS},
        )
        assert resp.status_code == 200
        assert resp.json()["verification_passed"] is False
        assert resp.json()["unknown_numbers"] == ["78"]

    def test_cache_fallback_is_labeled(self, client, monkeypatch):
        """라이브 호출이 죽어 캐시본을 쓸 때는 source='cache'로 드러나야 한다."""
        monkeypatch.setattr(
            "app.serving_api.narrate_event", lambda event, **kw: _fake_result(source="cache")
        )
        resp = client.post(
            "/api/anomalies/explain",
            json={"store_id": DANGER_STORE_ID, "detected_at": DANGER_TS},
        )
        assert resp.json()["source"] == "cache"

    def test_nonexistent_event_returns_404(self, client):
        """감지 이벤트가 없는 시각이면 404 - LLM을 부르기 전에 막는다."""
        resp = client.post(
            "/api/anomalies/explain",
            json={"store_id": DANGER_STORE_ID, "detected_at": "2026-05-09T19:15:00"},
        )
        assert resp.status_code == 404

    def test_missing_api_key_returns_503(self, client, monkeypatch):
        """키가 없으면 이 엔드포인트만 503이고 다른 API는 살아 있어야 한다."""
        def _raise(event, **kw):
            raise RuntimeError("NVIDIA_API_KEY가 설정되지 않았습니다 (db/.env 확인).")

        monkeypatch.setattr("app.serving_api.narrate_event", _raise)
        resp = client.post(
            "/api/anomalies/explain",
            json={"store_id": DANGER_STORE_ID, "detected_at": DANGER_TS},
        )
        assert resp.status_code == 503
        assert client.get("/api/stores").status_code == 200  # 다른 API는 정상

    def test_llm_failure_returns_502(self, client, monkeypatch):
        """LLM 호출/파싱 실패는 502로 구분해서 알린다(키 문제인 503과 다른 원인)."""
        def _raise(event, **kw):
            raise ValueError("JSON을 찾을 수 없습니다")

        monkeypatch.setattr("app.serving_api.narrate_event", _raise)
        resp = client.post(
            "/api/anomalies/explain",
            json={"store_id": DANGER_STORE_ID, "detected_at": DANGER_TS},
        )
        assert resp.status_code == 502

    def test_client_supplied_values_are_ignored(self, client, monkeypatch):
        """
        수치는 클라이언트가 아니라 DB에서 읽는다.

        body에 metric_value 같은 걸 끼워 넣어도 응답의 근거 수치가 바뀌면 안 된다
        (임의 값으로 그럴듯한 설명을 만들어내는 경로를 막는 것이 설계 의도).
        """
        captured = {}

        def _capture(event, **kw):
            captured.update(event)
            return _fake_result()

        monkeypatch.setattr("app.serving_api.narrate_event", _capture)
        resp = client.post(
            "/api/anomalies/explain",
            json={
                "store_id": DANGER_STORE_ID,
                "detected_at": DANGER_TS,
                "metric_value": 99999,      # 무시돼야 함
                "contract_power_kw": 99999,  # 무시돼야 함
            },
        )
        assert resp.status_code == 200
        assert captured["metric_value"] != 99999
        assert float(captured["contract_power_kw"]) == 45.0
