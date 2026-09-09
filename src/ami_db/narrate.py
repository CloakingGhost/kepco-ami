# -*- coding: utf-8 -*-
"""
안전감지 이벤트 -> 사람이 읽을 문장 생성 (LLM 설명 계층).

**이 모듈은 판정에 일절 관여하지 않는다.** 위험/주의 등급은 ami_db.anomaly의 규칙이
이미 확정한 것이고, 여기서는 그 확정된 결과를 사람 언어로 옮기기만 한다. 이 경계는
취향이 아니라 실험 결과다 - 같은 데이터로 규칙과 Isolation Forest를 비교했을 때
탐지 성능이 규칙 0.698 vs AI 0.582로 규칙이 더 나았고(feat/ai-necessity-verification
브랜치의 docs/AI_필요성_검증_결과.md), 무엇보다 KEC 212.3이라는 법정 근거를 확률
모델로 대체하면 "그래서 정확도가 몇 %입니까"에 답할 수 없게 된다.

반대로 "숫자를 사람 문장으로 옮기는 일"은 LLM이 잘하고, 결정적으로 **검증이 가능하다** -
생성된 문장에서 숫자를 뽑아 입력 수치와 대조하면 환각 여부를 기계적으로 판정할 수 있다
(verify_numbers). 라벨이 없어 성능 검증이 불가능했던 탐지 쪽과 달리, 이 계층은
"틀렸는지 아닌지"를 자동으로 확인할 수 있다는 점이 채택 근거다.

의존성을 늘리지 않으려고 표준 라이브러리 urllib만 쓴다(서버 배포 시 uv sync로
새 패키지를 받아야 하는 실패 지점을 만들지 않기 위함).
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime

from .config import CACHE_DIR, settings

API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
# 1순위는 짧게 끊고 대체 모델로 넘어가는 게 낫고(살아 있으면 3초면 끝난다), 대체 모델은
# 마지막 라이브 시도라 콜드스타트(실측 35초)를 견딜 만큼 넉넉히 준다.
PRIMARY_TIMEOUT_SEC = 20
FALLBACK_TIMEOUT_SEC = 50
CACHE_PATH = CACHE_DIR / "narration_cache.json"

SYSTEM_PROMPT = """당신은 전기안전 관제 시스템의 보고서 작성 보조입니다.

절대 규칙:
1. 입력으로 받은 숫자만 사용하십시오. 어떤 숫자도 새로 만들거나 계산해서 쓰지 마십시오.
2. 화재·누전·아크가 발생했다고 단정하지 마십시오. 이 시스템은 전력 사용 패턴의 이상만
   감지하며, 실제 사고 발생 여부는 알 수 없습니다.
3. 원인을 추정하지 마십시오. (예: "냉장고 고장으로 보입니다" 같은 표현 금지)
   관측된 사실만 기술하고, 점검을 권유하는 선에서 멈추십시오.
4. 반드시 아래 JSON만 출력하십시오. 코드블록 표시나 설명을 덧붙이지 마십시오.

{"owner_sms": "...", "admin_note": "...", "emergency_report": null}

- owner_sms: 점주에게 보낼 문자. 2~3문장, 존댓말. 불안을 조장하지 말고 점검을 권유하되,
  관측값과 임계값을 반드시 포함하십시오.
- admin_note: 관리자용 점검 사유. 1~2문장. 발동한 규칙과 근거 수치를 명시하십시오.
- emergency_report: 안전등급이 '위험'일 때만 신고 접수용 3~4문장으로 작성하고,
  '주의'이면 반드시 null로 두십시오."""


# rule_triggered -> 프롬프트에 넣을 한국어 규칙 설명. 코드값을 그대로 주면 모델이
# 규칙명을 임의로 풀어 쓰다가 근거를 왜곡할 수 있어, 근거 조문까지 문장으로 못박아 넘긴다.
# 문구는 docs/안전감지_이상치_판정기준.md 4절과 일치시켜야 한다.
RULE_DESCRIPTIONS = {
    "kec212_overload_130pct_60min":
        "KEC 212.3 표 212.3-2 산업용 배선차단기 기준 - 계약전력 130%가 60분 지속",
    "continuous_load_80pct_180min":
        "연속부하 80% 규칙(NEC 210.20(A)) - 계약전력 80%가 3시간 지속",
    "empty_store_baseline_3x_60min":
        "매장 자체 비영업시간 baseline 대비 초과 - 평소 수준에서 크게 벗어난 상태가 60분 지속",
}


def _as_float(value) -> float | None:
    """Postgres NUMERIC은 Decimal로 오므로 JSON 직렬화 전에 float로 눕힌다."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_ts(value) -> str:
    """datetime -> 'YYYY-MM-DD HH:MM' (LLM에 넣을 땐 초 단위가 불필요한 노이즈다)."""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


def build_prompt_payload(event: dict) -> dict:
    """
    DB에서 읽은 이벤트(영문 키) -> LLM에 넣을 한국어 키 payload.

    모델이 필드 의미를 오해하지 않도록 키 이름 자체를 한국어로 주고, 단위를 키에 박아둔다
    (kWh인지 kW인지 헷갈리면 문장에서 단위를 틀리게 쓴다). 수치는 소수 둘째 자리로 반올림해
    넘긴다 - 18.94857157639569를 그대로 주면 모델이 그 긴 숫자를 문장에 그대로 옮겨 적는다.
    """
    metric, threshold = _as_float(event.get("metric_value")), _as_float(event.get("threshold_value"))
    payload = {
        "매장명": event.get("store_name"),
        "업종": event.get("biz_category_mid") or event.get("biz_category_large"),
        "계약전력_kW": _as_float(event.get("contract_power_kw")),
        "안전등급": event.get("level"),
        "발동규칙": event.get("rule_text") or RULE_DESCRIPTIONS.get(
            event.get("rule_triggered", ""), event.get("rule_triggered", "")
        ),
        "감지시각": _fmt_ts(event.get("detected_at")),
        "관측_전력량_kWh_15분": round(metric, 2) if metric is not None else None,
        "임계_전력량_kWh_15분": round(threshold, 2) if threshold is not None else None,
        "판정_범위": "영업시간 외(비영업시간)",
    }
    if event.get("repeat_count"):
        payload["최근24시간_반복횟수"] = event["repeat_count"]
    return {k: v for k, v in payload.items() if v is not None}


@dataclass
class NarrationResult:
    owner_sms: str
    admin_note: str
    emergency_report: str | None
    model: str
    elapsed_ms: int
    verification_passed: bool
    unknown_numbers: list[str] = field(default_factory=list)
    raw_output: str = ""
    # "live"=이번에 생성, "cache"=이전 생성분 재사용(LLM API 장애 시). 화면·응답에 그대로
    # 노출해서 "지금 만든 문장인지 저장해둔 문장인지"를 감추지 않는다.
    source: str = "live"


def _cache_key(event: dict) -> str:
    return f"{event.get('store_id')}:{_fmt_ts(event.get('detected_at'))}"


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(key: str, result: "NarrationResult") -> None:
    cache = _load_cache()
    cache[key] = {
        "owner_sms": result.owner_sms,
        "admin_note": result.admin_note,
        "emergency_report": result.emergency_report,
        "model": result.model,
        "elapsed_ms": result.elapsed_ms,
        "verification_passed": result.verification_passed,
        "unknown_numbers": result.unknown_numbers,
    }
    try:
        CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass  # 캐시 저장 실패가 응답 자체를 막을 이유는 없다


def _number_tokens(text: str) -> list[str]:
    """문장에서 숫자 토큰만 추출. 천단위 콤마(1,234)는 하나로 묶어서 본다."""
    return re.findall(r"\d[\d,]*(?:\.\d+)?", text)


def _norm(token: str) -> float | None:
    try:
        return float(token.replace(",", ""))
    except ValueError:
        return None


def verify_numbers(text: str, allowed: set[float]) -> list[str]:
    """
    생성문에 입력에 없던 숫자가 섞였는지 검사해 그 목록을 돌려준다(빈 리스트 = 통과).

    LLM이 그럴듯한 수치를 지어내는 것이 이 기능의 유일한 실질 위험이므로, 배포 전에
    사람이 읽어보는 대신 기계가 매번 대조한다. 허용 집합은 그 이벤트에서 실제로
    파생된 값들(관측/임계/계약전력/시각 구성요소 등)로만 구성한다 - 넉넉하게 잡으면
    검사 자체가 무의미해지므로 의도적으로 좁게 잡고, 대신 소수점 표기 흔들림
    (18.95 vs 18.9)만 허용 오차로 흡수한다.
    """
    unknown: list[str] = []
    for token in _number_tokens(text):
        value = _norm(token)
        if value is None:
            continue
        # 표기 반올림 흔들림 허용: 허용값 중 하나와 0.5% 이내면 같은 값으로 본다.
        if any(abs(value - a) <= max(abs(a) * 0.005, 1e-9) for a in allowed):
            continue
        unknown.append(token)
    return unknown


def build_allowed_numbers(event: dict) -> set[float]:
    """
    검증용 허용 숫자 집합. 이벤트에서 실제로 파생되는 값 + 문장 구성에 불가피한
    구조적 상수(15분 슬롯, 규칙이 명시하는 비율/지속시간)만 넣는다.
    """
    allowed: set[float] = {15.0}  # AMI 계량 주기(15분)는 문장에 자연히 등장한다
    for key in ("contract_power_kw", "metric_value", "threshold_value", "repeat_count"):
        v = event.get(key)
        if v is not None:
            allowed.add(float(v))

    # 규칙이 스스로 명시하는 수치(130% / 60분 / 212.3 등)는 규칙 설명문에서 그대로 뽑아 허용한다.
    rule_text = event.get("rule_text") or RULE_DESCRIPTIONS.get(event.get("rule_triggered", ""), "")
    for token in _number_tokens(rule_text):
        v = _norm(token)
        if v is not None:
            allowed.add(v)

    # 감지 시각 구성요소(연/월/일/시/분)
    for token in _number_tokens(str(event.get("detected_at", ""))):
        v = _norm(token)
        if v is not None:
            allowed.add(v)

    # 관측/임계에서 자연스럽게 파생되는 표현(초과분, 배수)도 허용한다 - 모델이
    # "임계보다 4.32kWh 높다"처럼 쓰는 것은 지어낸 값이 아니라 계산된 값이다.
    metric, threshold = _as_float(event.get("metric_value")), _as_float(event.get("threshold_value"))
    if metric is not None and threshold:
        allowed.add(round(metric - threshold, 2))
        allowed.add(round(metric / threshold, 2))
        allowed.add(round(metric / threshold * 100, 1))

    # 15분 kWh <-> 순간 kW 환산(x4)도 허용한다. KEC는 kW로 말하는데 우리 임계값은
    # 15분 kWh로 저장돼 있어서, 모델이 "임계 14.62kWh = 58.5kW"처럼 환산해 쓰는 일이
    # 실제로 발생했다(프로덕션 실측). 이건 지어낸 값이 아니라 단위 변환이므로 허용하고,
    # 대신 출처를 추적할 수 없는 값(온도·피해액 등)은 계속 걸러낸다.
    for base in (metric, threshold):
        if base:
            allowed.add(round(base * 4, 2))
            allowed.add(round(base * 4, 1))

    # 계약전력 x 규칙 비율(예: 45kW의 130% = 58.5kW)도 같은 이유로 허용.
    contract = _as_float(event.get("contract_power_kw"))
    if contract:
        for token in _number_tokens(rule_text):
            ratio = _norm(token)
            if ratio and 1 < ratio <= 300:  # 130, 80 같은 퍼센트 표기만 대상
                allowed.add(round(contract * ratio / 100, 2))
    return allowed


def _extract_json(raw: str) -> dict:
    """```json 코드블록이나 앞뒤 잡담이 섞여 와도 JSON 본문만 건져낸다."""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"JSON을 찾을 수 없습니다: {raw[:200]}")
    return json.loads(text[start : end + 1])


def _post(body: bytes, api_key: str, timeout: int) -> dict:
    request = urllib.request.Request(
        API_URL,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _call_llm(event: dict, model: str, timeout: int = PRIMARY_TIMEOUT_SEC) -> tuple[str, int]:
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(build_prompt_payload(event), ensure_ascii=False, indent=2),
            },
        ],
        "temperature": 0.2,
        "max_tokens": 700,
    }).encode("utf-8")
    started = time.time()
    try:
        payload = _post(body, settings.nvidia_api_key, timeout)
    except urllib.error.HTTPError as e:
        # 키 문제로 보이는 응답이면 예비 키로 한 번 더. 그 외 오류(404/503 등)는
        # 모델 쪽 문제라 키를 바꿔도 소용없으므로 그대로 올려보내 상위에서 모델을 바꾼다.
        if e.code not in (401, 403, 429) or not settings.nvidia_api_key_2nd:
            raise
        payload = _post(body, settings.nvidia_api_key_2nd, timeout)
    elapsed_ms = int((time.time() - started) * 1000)
    return payload["choices"][0]["message"]["content"], elapsed_ms


def _narrate_with_model(event: dict, model: str, timeout: int) -> NarrationResult:
    raw, elapsed_ms = _call_llm(event, model, timeout)
    parsed = _extract_json(raw)

    owner_sms = str(parsed.get("owner_sms", "")).strip()
    admin_note = str(parsed.get("admin_note", "")).strip()
    emergency = parsed.get("emergency_report")
    emergency_report = str(emergency).strip() if emergency else None
    if not owner_sms or not admin_note:
        raise ValueError(f"필수 필드가 비어 있습니다: {raw[:200]}")

    allowed = build_allowed_numbers(event)
    unknown = verify_numbers(
        " ".join(filter(None, [owner_sms, admin_note, emergency_report])), allowed
    )
    return NarrationResult(
        owner_sms=owner_sms,
        admin_note=admin_note,
        emergency_report=emergency_report,
        model=model,
        elapsed_ms=elapsed_ms,
        verification_passed=not unknown,
        unknown_numbers=unknown,
        raw_output=raw,
        source="live",
    )


def narrate_event(event: dict, model: str | None = None) -> NarrationResult:
    """
    이벤트 dict -> 점주 문자/관리자 사유/(위험이면) 신고 초안 + 숫자 검증 결과.

    event는 DB에서 조회한 값으로 채워야 한다(클라이언트가 보낸 수치를 그대로 쓰면
    임의의 값을 LLM에 먹일 수 있어 설명의 신뢰가 무너진다 - serving.get_anomaly_event 참고).

    3단 방어: 1순위 모델 -> 대체 모델 -> 캐시. 호스팅 LLM은 예고 없이 응답 불능이 되므로
    (실측: mistral-nemotron이 2.8초에서 60초 타임아웃으로 급변) 라이브 호출 실패가 곧
    화면 실패가 되지 않게 한다. 캐시본을 쓸 때는 source="cache"로 그 사실을 드러낸다.
    """
    if not settings.nvidia_api_key:
        raise RuntimeError("NVIDIA_API_KEY가 설정되지 않았습니다 (db/.env 확인).")

    candidates = [model] if model else [settings.nvidia_model, settings.nvidia_model_fallback]
    errors: list[str] = []
    for position, candidate in enumerate([c for c in candidates if c]):
        timeout = PRIMARY_TIMEOUT_SEC if position == 0 else FALLBACK_TIMEOUT_SEC
        try:
            result = _narrate_with_model(event, candidate, timeout)
        except Exception as e:  # noqa: BLE001 - 어떤 실패든 다음 후보로 넘어간다
            errors.append(f"{candidate}: {type(e).__name__}")
            continue
        _save_cache(_cache_key(event), result)
        return result

    cached = _load_cache().get(_cache_key(event))
    if cached:
        return NarrationResult(**cached, raw_output="", source="cache")

    raise RuntimeError(f"설명문 생성 실패(캐시도 없음) - 시도: {', '.join(errors)}")
