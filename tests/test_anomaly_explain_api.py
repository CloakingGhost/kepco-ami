# -*- coding: utf-8 -*-
"""
AI 분석 계층(POST /api/anomalies/explain, /api/anomalies/explain/status) 검증.

**외부 LLM API를 실제로 호출하지 않는다.** 호출 결과가 매번 달라 회귀 테스트로 쓸 수
없고(실측에서 1순위 모델이 2.8초 -> 무응답으로 급변했다), 여기서 확인해야 할 것은 모델의
문장력이 아니라 **우리 코드의 계약**이기 때문이다:

  - 환각 검증기가 지어낸 숫자를 잡아내는가 / 추적 가능한 값을 오탐하지 않는가
  - 숫자 검증에 실패한 결과는 버리고 다음 모델로 넘어가는가
  - 분석은 요청했을 때만 만들어지고, 만든 결과는 DB에 저장돼 다음 요청에서 AI 호출 없이 읽히는가
  - AI 실패·키 없음이 HTTP 에러가 아니라 status='failed' + 안내문으로 오는가
  - 같은 이벤트를 연달아 요청해도 작업이 중복으로 돌지 않는가
  - 수치를 클라이언트 입력이 아니라 DB에서 읽는가

anomaly_narrations에 실제로 쓰기 때문에, 테스트 대상 이벤트의 기존 분석 행은 테스트 전에
보관했다가 끝나면 되돌린다(로컬 DB에서 사람이 요청해 둔 분석을 테스트가 지우지 않도록).

실제 모델 품질 비교는 docs/LLM_모델선정_비교실험.md, 프로덕션 실호출 결과는
docs/LLM_설명API_검증결과.md 참고.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import text

import ami_db.narrate as narrate
from ami_db.config import settings
from ami_db.narrate import (
    NarrationResult,
    NarrationUnavailable,
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

EXPLAIN = "/api/anomalies/explain"
STATUS = "/api/anomalies/explain/status"


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
        검증 실패로 찍혀 분석 자체가 버려진다.
        """
        assert verify_numbers(text, allowed) == []

    def test_rounding_tolerance(self, allowed):
        """18.94857...을 18.95로 반올림해 써도 통과해야 한다(표기 흔들림 허용)."""
        assert verify_numbers("관측값은 18.95kWh입니다", allowed) == []
        # 다만 허용 오차(0.5%)를 벗어나면 잡힌다
        assert verify_numbers("관측값은 19.5kWh입니다", allowed) == ["19.5"]

    def test_small_threshold_quoted_exactly_passes(self):
        """
        작은 임계값을 **정확히 인용한** 문장이 환각으로 찍히면 안 된다.

        프롬프트에는 round(x, 2)로 넣으므로 모델이 그대로 받아 적으면 원본과 최대
        0.005 벌어진다. 비율 오차(0.5%)만 쓰던 시절 0.4649 -> "0.46"이 0.0049 차이로
        걸려서, 임계값을 제대로 인용한 설명이 통째로 버려졌다(실측 2026-09-10).
        """
        event = {
            **DANGER_EVENT,
            "rule_triggered": "empty_store_baseline_3x_60min",
            "metric_value": 0.9412,
            "threshold_value": 0.4649,
        }
        text = "관측 0.94 kWh/15분이 임계 0.46 kWh/15분을 초과했습니다"
        assert verify_numbers(text, build_allowed_numbers(event)) == []

    def test_minutes_expressed_as_hours_passes(self, allowed):
        """
        규칙 문구는 "60분 지속"인데 점주용 문장은 "1시간"으로 쓰는 게 자연스럽다.
        단위를 바꿔 적은 것이므로 환각이 아니다.
        """
        assert verify_numbers("1시간 넘게 계속 쓰였습니다", allowed) == []

    def test_widened_tolerance_still_catches_fabrication(self, allowed):
        """허용 범위를 넓힌 뒤에도 지어낸 값은 그대로 걸려야 한다(넓히기의 안전장치)."""
        assert verify_numbers("내부 온도 78도, 피해액 350만원", allowed) == ["78", "350"]


class TestPromptPayload:
    def test_datetime_and_decimal_are_serializable(self):
        """DB에서 오는 datetime/Decimal이 그대로 들어가면 JSON 직렬화가 깨진다(실제 발생한 버그)."""
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
# 모델 폴백 - 검증을 통과한 결과만 돌려주는가
# ============================================================

GOOD_OUTPUT = json.dumps({
    "owner_sms": "어젯밤 문을 닫으신 시간에 전기가 많이 쓰였습니다. 점검을 권해드립니다.",
    "admin_note": "KEC 212.3 기준 계약전력 130%가 60분 지속.",
    "next_steps": ["전기 점검을 받아보세요."],
}, ensure_ascii=False)

HALLUCINATED_OUTPUT = json.dumps({
    "owner_sms": "예상 피해액이 350만원입니다.",
    "admin_note": "내부 온도가 78도까지 올랐습니다.",
}, ensure_ascii=False)


@pytest.fixture
def two_models(monkeypatch):
    monkeypatch.setattr(settings, "nvidia_api_key", "test-key")
    monkeypatch.setattr(settings, "nvidia_model", "primary")
    monkeypatch.setattr(settings, "nvidia_model_fallback", "fallback")


def _llm_returning(outputs: dict):
    return lambda event, model, timeout: (outputs[model], 10)


class TestNarrateEvent:
    def test_verification_failure_moves_to_next_model(self, monkeypatch, two_models):
        """
        1순위가 숫자를 지어내면 그 결과를 버리고 대체 모델로 넘어간다.
        검증 실패본을 저장하면 재생성 기능이 없는 이상 그 이벤트는 영영 빈칸이 된다.
        """
        monkeypatch.setattr(narrate, "_call_llm", _llm_returning(
            {"primary": HALLUCINATED_OUTPUT, "fallback": GOOD_OUTPUT}
        ))
        result = narrate.narrate_event(DANGER_EVENT)
        assert result.model == "fallback"
        assert result.verification_passed is True

    def test_all_models_failing_raises_unavailable(self, monkeypatch, two_models):
        monkeypatch.setattr(narrate, "_call_llm", _llm_returning(
            {"primary": HALLUCINATED_OUTPUT, "fallback": HALLUCINATED_OUTPUT}
        ))
        with pytest.raises(NarrationUnavailable, match="숫자검증실패"):
            narrate.narrate_event(DANGER_EVENT)

    def test_missing_key_is_distinguished_from_model_failure(self, monkeypatch):
        """키 없음은 RuntimeError, 모델 실패는 NarrationUnavailable - 기록에서 원인을 구분하려고."""
        monkeypatch.setattr(settings, "nvidia_api_key", "")
        with pytest.raises(RuntimeError) as exc:
            narrate.narrate_event(DANGER_EVENT)
        assert not isinstance(exc.value, NarrationUnavailable)


# ============================================================
# 엔드포인트 - 요청 시 생성 -> DB 저장 -> 다음부터 DB에서 읽기
# (LLM은 monkeypatch로 대체, DB는 실제 Postgres)
# ============================================================

def _fake_result(**overrides) -> NarrationResult:
    base = {
        "owner_sms": "어젯밤 문을 닫으신 시간에 전기가 많이 쓰였습니다. 점검을 권해드립니다.",
        "admin_note": "KEC 212.3 기준 계약전력 130%가 60분 지속.",
        "emergency_report": None,
        "next_steps": ["전기 점검을 받아보세요."],
        "model": "test-model",
        "elapsed_ms": 10,
        "verification_passed": True,
        "unknown_numbers": [],
    }
    base.update(overrides)
    return NarrationResult(**base)


def _body(store_id=DANGER_STORE_ID, ts=DANGER_TS, **extra):
    return {"store_id": store_id, "detected_at": ts, **extra}


_JSONB_COLUMNS = ("next_steps", "unknown_numbers")


@pytest.fixture
def event_ids(engine):
    """테스트 대상 두 이벤트의 event_id."""
    query = text(
        """
        SELECT s.store_id, ae.event_id FROM anomaly_events ae
        JOIN stores s ON s.meter_id = ae.meter_id
        WHERE (s.store_id = :s1 AND ae.detected_at = :t1)
           OR (s.store_id = :s2 AND ae.detected_at = :t2)
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(query, {
            "s1": DANGER_STORE_ID, "t1": datetime.fromisoformat(DANGER_TS),
            "s2": CAUTION_STORE_ID, "t2": datetime.fromisoformat(CAUTION_TS),
        }).all()
    ids = {store_id: event_id for store_id, event_id in rows}
    assert len(ids) == 2, "테스트용 데모 이벤트가 DB에 없습니다(06/10단계 적재 확인)"
    return {"danger": ids[DANGER_STORE_ID], "caution": ids[CAUTION_STORE_ID]}


@pytest.fixture
def clean_narrations(engine, event_ids):
    """
    대상 이벤트의 분석 행을 비운 채로 시작하고, 끝나면 원래 행을 되돌린다.
    로컬 DB에서 사람이 화면으로 요청해 둔 분석을 테스트가 지워 버리지 않기 위함이다.
    """
    ids = list(event_ids.values())
    with engine.begin() as conn:
        saved = [dict(r) for r in conn.execute(
            text("SELECT * FROM anomaly_narrations WHERE event_id = ANY(:ids)"), {"ids": ids}
        ).mappings()]
        conn.execute(text("DELETE FROM anomaly_narrations WHERE event_id = ANY(:ids)"), {"ids": ids})
    yield event_ids
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM anomaly_narrations WHERE event_id = ANY(:ids)"), {"ids": ids})
        for row in saved:
            cols = list(row)
            values = ", ".join(
                f"CAST(:{c} AS jsonb)" if c in _JSONB_COLUMNS else f":{c}" for c in cols
            )
            params = {
                c: json.dumps(row[c], ensure_ascii=False) if c in _JSONB_COLUMNS else row[c]
                for c in cols
            }
            conn.execute(
                text(f"INSERT INTO anomaly_narrations ({', '.join(cols)}) VALUES ({values})"),
                params,
            )


@pytest.fixture
def ai_on(monkeypatch):
    """키가 있는 상태로 고정한다(로컬 .env 유무와 무관하게 결과가 같도록)."""
    monkeypatch.setattr(settings, "nvidia_api_key", "test-key")


def _narration_row(engine, event_id):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT status, error_message FROM anomaly_narrations WHERE event_id = :id"),
            {"id": event_id},
        ).mappings().first()


def _counting(calls: dict):
    def _fn(event):
        calls["n"] += 1
        return _fake_result()
    return _fn


@pytest.mark.usefixtures("clean_narrations", "ai_on")
class TestAnalysisFlow:
    def test_status_before_any_request_is_none(self, client):
        body = client.post(STATUS, json=_body()).json()
        assert body["status"] == "none"
        assert body["message"]
        assert body["owner_sms"] is None

    def test_status_endpoint_never_starts_generation(self, client, monkeypatch):
        """상태 조회는 읽기만 한다 - 여기서 AI가 불리면 폴링할 때마다 생성이 걸린다."""
        calls = {"n": 0}
        monkeypatch.setattr("app.serving_api.narrate_event", _counting(calls))
        client.post(STATUS, json=_body())
        assert calls["n"] == 0

    def test_request_generates_then_saves_to_db(self, client, monkeypatch, engine, event_ids):
        """요청 -> 즉시 pending 응답 -> 백그라운드 생성 -> DB 저장 -> 상태 조회로 done 확인."""
        monkeypatch.setattr(
            "app.serving_api.narrate_event",
            lambda event: _fake_result(emergency_report="긴급 점검이 필요합니다."),
        )
        first = client.post(EXPLAIN, json=_body())
        assert first.status_code == 200
        assert first.json()["status"] == "pending"  # 생성은 응답을 보낸 뒤에 돈다
        assert first.json()["message"]

        done = client.post(STATUS, json=_body()).json()
        assert done["status"] == "done"
        assert done["message"] is None
        # 등급과 규칙은 LLM이 아니라 DB(규칙 판정 결과)에서 온다
        assert done["level"] == "위험"
        assert done["rule_triggered"] == "kec212_overload_130pct_60min"
        assert done["store_name"] == "충북식당"
        assert done["owner_sms"] and done["admin_note"]
        assert done["emergency_report"] == "긴급 점검이 필요합니다."
        assert done["next_steps"] == ["전기 점검을 받아보세요."]
        assert done["verification_passed"] is True
        assert _narration_row(engine, event_ids["danger"])["status"] == "done"

    def test_stored_analysis_is_returned_without_calling_ai(self, client, monkeypatch):
        """한 번 만든 분석은 DB에서 그대로 읽는다 - 같은 이벤트로 AI를 다시 부르지 않는다."""
        monkeypatch.setattr("app.serving_api.narrate_event", lambda event: _fake_result())
        client.post(EXPLAIN, json=_body())

        calls = {"n": 0}
        monkeypatch.setattr("app.serving_api.narrate_event", _counting(calls))
        again = client.post(EXPLAIN, json=_body()).json()
        assert again["status"] == "done"  # pending을 거치지 않고 즉시
        assert again["owner_sms"]
        assert calls["n"] == 0

    def test_ai_failure_is_status_not_http_error(self, client, monkeypatch, engine, event_ids):
        """외부 모델이 전부 죽어도 에러 화면이 아니라 '실패했으니 다시 요청' 안내가 가야 한다."""
        def _dead(event):
            raise NarrationUnavailable("설명문 생성 실패 - 시도: primary: TimeoutError")

        monkeypatch.setattr("app.serving_api.narrate_event", _dead)
        assert client.post(EXPLAIN, json=_body()).status_code == 200

        status = client.post(STATUS, json=_body())
        assert status.status_code == 200
        assert status.json()["status"] == "failed"
        assert "다시 요청" in status.json()["message"]
        # 원인은 운영 기록에만 남기고 화면(응답)에는 보내지 않는다
        assert "NarrationUnavailable" in _narration_row(engine, event_ids["danger"])["error_message"]
        assert "TimeoutError" not in status.json()["message"]

    def test_failed_analysis_can_be_requested_again(self, client, monkeypatch):
        """외부 모델 장애는 일시적이다 - 실패한 뒤 다시 요청하면 새로 시도한다."""
        def _dead(event):
            raise NarrationUnavailable("down")

        monkeypatch.setattr("app.serving_api.narrate_event", _dead)
        client.post(EXPLAIN, json=_body())
        assert client.post(STATUS, json=_body()).json()["status"] == "failed"

        monkeypatch.setattr("app.serving_api.narrate_event", lambda event: _fake_result())
        client.post(EXPLAIN, json=_body())
        assert client.post(STATUS, json=_body()).json()["status"] == "done"

    def test_missing_api_key_is_message_and_writes_nothing(
        self, client, monkeypatch, engine, event_ids
    ):
        """키가 없으면 안내만 돌려주고 DB에는 아무것도 남기지 않는다. 다른 API는 정상."""
        monkeypatch.setattr(settings, "nvidia_api_key", "")
        resp = client.post(EXPLAIN, json=_body())
        assert resp.status_code == 200
        assert resp.json()["status"] == "failed"
        assert "사용할 수 없습니다" in resp.json()["message"]
        assert _narration_row(engine, event_ids["danger"]) is None
        assert client.get("/api/stores").status_code == 200

    def test_request_while_in_progress_does_not_start_second_job(
        self, client, monkeypatch, engine, event_ids
    ):
        """생성 중에 또 누르거나 다른 사람이 눌러도 AI 호출은 하나만 돈다."""
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO anomaly_narrations (event_id, status) VALUES (:id, 'pending')"),
                {"id": event_ids["danger"]},
            )
        calls = {"n": 0}
        monkeypatch.setattr("app.serving_api.narrate_event", _counting(calls))

        assert client.post(EXPLAIN, json=_body()).json()["status"] == "pending"
        assert calls["n"] == 0

    def test_stale_pending_is_restarted(self, client, monkeypatch, engine, event_ids):
        """생성 중 서버가 재기동돼 사라진 작업은 일정 시간 뒤 '실패'로 보이고 다시 요청할 수 있다."""
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO anomaly_narrations (event_id, status, requested_at) "
                     "VALUES (:id, 'pending', now() - interval '10 minutes')"),
                {"id": event_ids["danger"]},
            )
        assert client.post(STATUS, json=_body()).json()["status"] == "failed"

        monkeypatch.setattr("app.serving_api.narrate_event", lambda event: _fake_result())
        client.post(EXPLAIN, json=_body())
        assert client.post(STATUS, json=_body()).json()["status"] == "done"

    def test_caution_event_has_no_emergency_report(self, client, monkeypatch):
        """주의 등급이면 신고 초안은 null이어야 한다."""
        monkeypatch.setattr("app.serving_api.narrate_event", lambda event: _fake_result())
        client.post(EXPLAIN, json=_body(CAUTION_STORE_ID, CAUTION_TS))
        body = client.post(STATUS, json=_body(CAUTION_STORE_ID, CAUTION_TS)).json()
        assert body["level"] == "주의"
        assert body["emergency_report"] is None

    def test_client_supplied_values_are_ignored(self, client, monkeypatch):
        """
        수치는 클라이언트가 아니라 DB에서 읽는다.

        body에 metric_value 같은 걸 끼워 넣어도 AI에 들어가는 근거 수치가 바뀌면 안 된다
        (임의 값으로 그럴듯한 설명을 만들어내는 경로를 막는 것이 설계 의도).
        """
        captured = {}

        def _capture(event):
            captured.update(event)
            return _fake_result()

        monkeypatch.setattr("app.serving_api.narrate_event", _capture)
        resp = client.post(EXPLAIN, json=_body(metric_value=99999, contract_power_kw=99999))
        assert resp.status_code == 200
        assert captured["metric_value"] != 99999
        assert float(captured["contract_power_kw"]) == 45.0


class TestNotFound:
    @pytest.mark.parametrize("path", [EXPLAIN, STATUS])
    def test_nonexistent_event_returns_404(self, client, path):
        """감지 이벤트 자체가 없는 시각이면 404 - 이것만은 진짜 요청 오류다."""
        resp = client.post(path, json=_body(ts="2026-05-09T19:15:00"))
        assert resp.status_code == 404
