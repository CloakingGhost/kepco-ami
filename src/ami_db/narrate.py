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

이 모듈은 "이벤트 -> 검증된 문장"까지만 책임진다. 언제 만들지(사용자가 요청했을 때만)와
만든 결과를 어디에 둘지(DB anomaly_narrations)는 호출부 몫이다 - app/serving_api.py의
백그라운드 작업과 ami_db.serving의 저장 함수 참고.

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

from .config import settings

API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
# 생성은 요청 핸들러가 아니라 **백그라운드 작업**에서 돈다(app/serving_api.py 참고). 그래서
# 앞단 프록시(30초)가 아니라 모델 응답 시간이 예산을 정한다 - 2026-09-10 실측으로 살아 있던
# 모델이 24~57초(gemma는 한 번 73초)였다. 예전엔 요청을 붙잡고 27초 안에 끝내려다 거의 매번
# 실패했고, 그 실패가 사용자에게 에러 화면으로 나갔다.
#
# 예산은 모델별로 따로 주지 않고 **전체 한도를 나눠 쓴다.** 1순위가 404로 0.2초 만에 죽는
# 날엔 대체 모델이 남은 시간을 다 쓸 수 있고, 1순위가 끝까지 물고 늘어져도 전체가 이 한도를
# 넘지 않는다. 합계는 serving.NARRATION_STALE_MINUTES(5분)보다 짧아야 한다 - 그보다 길면
# 멀쩡히 진행 중인 작업을 "사라진 작업"으로 오판해 중복 실행한다.
JOB_BUDGET_SEC = 180
PER_MODEL_TIMEOUT_SEC = 90  # 한 모델이 예산을 전부 삼키지 않도록 하는 상한
MIN_ATTEMPT_SEC = 10        # 이보다 적게 남았으면 시도해봐야 의미가 없다

# 점주 안내에 쓸 수 있는 사실만 모아둔 참고 자료.
#
# 왜 프롬프트에 박아 넣는가: 모델이 제도·절차·비용을 스스로 알고 있다고 믿으면 안 된다.
# 숫자 환각은 verify_numbers()가 잡지만 "이런 제도가 있습니다" 같은 서술형 환각은 기계가
# 잡을 수 없어서, 애초에 근거 자료를 입력으로 주고 "여기 있는 것만 쓰라"고 못박는다.
# 출처: 배달의민족 사장님센터(ceo.baemin.com/knowhow/11020),
#       요기요 파트너(partner.yogiyo.co.kr, 음식점 전기요금·계약전력 계산방법),
#       한국전력공사 고객센터 안내(www.kepco.co.kr).
OWNER_GUIDANCE = """- 계약전력은 한전이 그 매장에 공급하기로 약속한 전력 용량이며, 매장 배선·차단기가
  그 용량에 맞춰 설치되어 있다.
- 계약전력을 넘겨 쓰는 일이 반복되면 한전에서 먼저 증설 안내문을 보내고, 그 뒤로도
  반복되면 초과요금이 붙을 수 있다.
- 증설은 두 가지다. 건물에 여유 용량이 남아 있으면 공사 없이 서류(건물주 날인,
  임대차계약서)만으로 처리되고, 여유가 없으면 분전함 교체 같은 내선공사가 필요하다.
- 24시간 운영하는 매장은 한전에 '720시간 특례'를 신청하면 기준시간이 늘어 초과 판정이
  줄어들 수 있다.
- 계약전력 20kW 이상인 매장은 한전도 15분 단위로 피크 전력을 확인한다. 전자레인지·
  커피머신처럼 큰 기기를 동시에 켜면 순간 사용량이 치솟는다.
- 문의처: 한국전력공사 고객센터(국번 없이 123, 24시간), 한전 사이버지점(cyber.kepco.co.kr)."""

SYSTEM_PROMPT = """당신은 전기안전 관제 시스템의 보고서 작성 보조입니다.

절대 규칙:
1. 입력으로 받은 숫자만 사용하십시오. 어떤 숫자도 새로 만들거나 계산해서 쓰지 마십시오.
2. 화재·누전·아크가 발생했다고 단정하지 마십시오. 이 시스템은 전력 사용 패턴의 이상만
   감지하며, 실제 사고 발생 여부는 알 수 없습니다.
3. 원인을 추정하지 마십시오. (예: "냉장고 고장으로 보입니다" 같은 표현 금지)
   관측된 사실만 기술하고, 점검을 권유하는 선에서 멈추십시오.
4. 제도·절차·비용은 입력의 '참고_안내사항'에 있는 내용만 쓰십시오. 거기 없는 제도나
   금액을 지어내지 마십시오.
5. 'baseline' 같은 영어 단어는 쓰지 말고 '평소 전력 수준'처럼 누구나 알아듣는 한국어로
   쓰십시오. (KEC·kWh 같은 규격명·단위는 그대로 둡니다.)
6. 반드시 아래 JSON만 출력하십시오. 코드블록 표시나 설명을 덧붙이지 마십시오.

{"owner_sms": "...", "admin_note": "...", "emergency_report": null,
 "next_steps": ["...", "..."], "inquiry_draft": "..."}

- owner_sms: 점주에게 보낼 문자. 2~3문장, 존댓말.
  **전기를 잘 모르는 분이 읽는다고 가정하십시오.** 숫자와 전문용어 대신 무슨 일이
  일어나고 있는지를 일상어로 쓰고, 겁을 주는 대신 무엇을 하면 되는지로 끝내십시오.
  나쁜 예: "전력 사용량이 18.95kWh로 계약전력(45kW)의 130%를 초과했습니다."
  좋은 예: "어젯밤 문을 닫으신 시간에, 매장 전기설비가 감당하도록 되어 있는 양보다
  훨씬 많은 전기가 한 시간 넘게 계속 쓰였습니다. 누전이나 과열일 수 있어 전기 점검을
  받아보시길 권해드립니다."
- admin_note: 관리자·전기담당자용. 1~2문장. 여기는 반대로 전문용어를 그대로 쓰고,
  발동한 규칙 이름과 근거 수치(관측값·임계값)를 정확히 명시하십시오.
- emergency_report: 안전등급이 '위험'일 때만 신고 접수용 3~4문장으로 작성하고,
  '주의'이면 반드시 null로 두십시오.
- next_steps: 점주가 지금 할 수 있는 일 2~3가지. **각 항목은 "무엇을 왜"가 드러나는
  완결된 한 문장**으로 쓰십시오("전기 점검 요청"처럼 명사로 끊지 마십시오).
  '참고_안내사항'에 근거한 것만 쓰고, 매장 상황에 맞는 것만 고르십시오.
  예: "냉장고·에어컨처럼 큰 기기가 동시에 돌지 않도록 시간을 나눠 쓰시면 순간 사용량을
  낮출 수 있습니다." / "건물에 여유 용량이 남아 있으면 공사 없이 서류만으로 용량을 늘릴
  수 있으니, 한전 고객센터(123)에 증설 대상인지 문의해 보세요."
- inquiry_draft: 점주가 한전이나 전기공사 업체에 그대로 보낼 수 있는 문의 초안.
  3~4문장. 매장명과 관측된 상황을 포함하되, 용어는 쉽게 쓰고 무엇을 알고 싶은지를
  분명히 하십시오(예: 우리 매장이 증설 대상인지, 공사가 필요한지)."""


# rule_triggered -> 프롬프트에 넣을 한국어 규칙 설명. 코드값을 그대로 주면 모델이
# 규칙명을 임의로 풀어 쓰다가 근거를 왜곡할 수 있어, 근거 조문까지 문장으로 못박아 넘긴다.
# 문구는 docs/안전감지_이상치_판정기준.md 4절과 일치시켜야 한다.
RULE_DESCRIPTIONS = {
    "kec212_overload_130pct_60min":
        "KEC 212.3 표 212.3-2 산업용 배선차단기 기준 - 계약전력 130%가 60분 지속",
    "continuous_load_80pct_180min":
        "연속부하 80% 규칙(NEC 210.20(A)) - 계약전력 80%가 3시간 지속",
    "empty_store_baseline_3x_60min":
        "빈 매장 시간대의 평소 전력 수준보다 크게 높은 상태가 60분 지속(매장별 기준)",
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
        # 대처방안(next_steps)·문의초안(inquiry_draft)의 근거. 모델이 제도를 스스로
        # 안다고 믿지 않고 사실을 통째로 넣어준다 - OWNER_GUIDANCE 주석 참고.
        "참고_안내사항": OWNER_GUIDANCE,
    }
    if event.get("repeat_count"):
        payload["최근24시간_반복횟수"] = event["repeat_count"]
    return {k: v for k, v in payload.items() if v is not None}


class NarrationUnavailable(RuntimeError):
    """
    외부 모델이 모두 실패해(호출 오류·파싱 실패·숫자 검증 실패) 설명문을 만들 수 없는 상태.

    "API 키가 없다"(RuntimeError)와 구분하려고 따로 둔다. 둘 다 사용자에게는 HTTP 에러가
    아니라 status='failed' 안내로 전달되지만, 운영 기록(anomaly_narrations.error_message)을
    보고 원인을 오해하지 않게 한다.
    """


@dataclass
class NarrationResult:
    owner_sms: str
    admin_note: str
    emergency_report: str | None
    model: str
    elapsed_ms: int
    verification_passed: bool
    # 점주가 "그래서 뭘 어쩌라는 거지?"에 답하는 부분.
    next_steps: list[str] = field(default_factory=list)
    inquiry_draft: str | None = None
    unknown_numbers: list[str] = field(default_factory=list)
    raw_output: str = ""


def _number_tokens(text: str) -> list[str]:
    """문장에서 숫자 토큰만 추출. 천단위 콤마(1,234)는 하나로 묶어서 본다."""
    return re.findall(r"\d[\d,]*(?:\.\d+)?", text)


def _norm(token: str) -> float | None:
    try:
        return float(token.replace(",", ""))
    except ValueError:
        return None


# 프롬프트에 넣는 수치는 build_prompt_payload가 소수 둘째 자리로 반올림한다. 그래서 모델이
# 그대로 받아 적은 값도 원본과 최대 0.005만큼 벌어질 수 있다(0.4649 -> "0.46").
# 비율 오차(0.5%)만 쓰면 작은 값에서 이 폭을 못 덮는다 - 0.46의 0.5%는 0.0023뿐이라
# **임계값을 정확히 인용한 문장이 환각으로 찍혔다**(실측: 0.46 / 0.66). 검증에 실패한
# 문장은 저장하지 않으므로, 이 오탐은 멀쩡한 분석을 조용히 버리게 만든다.
ROUNDING_TOLERANCE = 0.005


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
        # 표기 반올림 흔들림 허용: 허용값 중 하나와 0.5% 또는 0.005(둘 중 큰 쪽) 이내면
        # 같은 값으로 본다. 큰 값은 비율이, 작은 값은 절대 오차가 지배한다.
        if any(
            abs(value - a) <= max(abs(a) * 0.005, ROUNDING_TOLERANCE) for a in allowed
        ):
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
            value = float(v)
            allowed.add(value)
            # **모델에게 실제로 준 형태**도 함께 허용한다. build_prompt_payload가 소수
            # 둘째 자리로 반올림해서 넘기므로, 모델이 정확히 받아 적으면 원본이 아니라
            # 이 값이 문장에 나온다. 원본만 허용 집합에 넣으면 그 인용이 환각으로 찍힌다.
            allowed.add(round(value, 2))
            allowed.add(round(value, 1))

    # 규칙이 스스로 명시하는 수치(130% / 60분 / 212.3 등)는 규칙 설명문에서 그대로 뽑아 허용한다.
    rule_text = event.get("rule_text") or RULE_DESCRIPTIONS.get(event.get("rule_triggered", ""), "")
    for token in _number_tokens(rule_text):
        v = _norm(token)
        if v is not None:
            allowed.add(v)

    # 지속시간의 분<->시간 환산도 허용한다. 규칙 문구는 "60분 지속"이라고 적지만 점주가
    # 읽을 문장은 "한 시간 넘게"/"1시간 이상"으로 쓰는 게 자연스럽고, 실제로 그렇게 나온다.
    # 단위를 바꿔 적은 것을 환각으로 세면 멀쩡한 문장이 버려진다(실측: '1').
    for minutes in re.findall(r"(\d+)\s*분", rule_text):
        allowed.add(int(minutes) / 60)

    # 감지 시각 구성요소(연/월/일/시/분)
    for token in _number_tokens(str(event.get("detected_at", ""))):
        v = _norm(token)
        if v is not None:
            allowed.add(v)

    # 참고 안내사항에 든 수치(고객센터 123, 720시간 특례, 20kW 기준 등)도 입력의 일부이므로
    # 허용한다. 이걸 빼면 대처방안 문장이 통째로 검증 실패로 찍혀 검증이 무의미해진다.
    for token in _number_tokens(OWNER_GUIDANCE):
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


def _call_llm(event: dict, model: str, timeout: int = PER_MODEL_TIMEOUT_SEC) -> tuple[str, int]:
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
        # 필드가 5개(문자·점검사유·신고초안·대처방안·문의초안)로 늘었고 한국어는 토큰을
        # 많이 먹는다. 700 -> 1400으로 한 번 올렸는데도 부족했다: 2026-09-10 실측에서
        # nemotron-3-super가 1685~1744 토큰, gpt-oss-20b가 1367 토큰을 썼다. 1400에서
        # 잘리면 JSON이 중간에 끊겨 **파싱 자체가 실패**하고, 그게 "모델이 응답을 안 한다"로
        # 오인됐다. 실측 최대치의 1.4배로 잡는다.
        "max_tokens": 2400,
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


def _parse_and_verify(event: dict, raw: str, model: str, elapsed_ms: int) -> NarrationResult:
    """
    모델 원문 -> 필드 추출 + 숫자 검증. 호출(_call_llm)과 분리해 둔 이유는
    scripts/19_langgraph_narration_pipeline.py가 "호출"과 "검사"를 별도 노드로 보여주면서도
    이 로직을 재구현하지 않고 그대로 부르게 하기 위함이다.
    """
    parsed = _extract_json(raw)

    owner_sms = str(parsed.get("owner_sms", "")).strip()
    admin_note = str(parsed.get("admin_note", "")).strip()
    emergency = parsed.get("emergency_report")
    emergency_report = str(emergency).strip() if emergency else None
    if not owner_sms or not admin_note:
        raise ValueError(f"필수 필드가 비어 있습니다: {raw[:200]}")

    # 리스트가 아니라 문자열 하나로 오는 경우가 있어 형태를 맞춰준다.
    raw_steps = parsed.get("next_steps") or []
    if isinstance(raw_steps, str):
        raw_steps = [raw_steps]
    next_steps = [str(s).strip() for s in raw_steps if str(s).strip()]

    inquiry = parsed.get("inquiry_draft")
    inquiry_draft = str(inquiry).strip() if inquiry else None

    # 검증 대상에 대처방안·문의초안도 포함한다 - 오히려 이쪽이 제도·금액을 지어내기
    # 쉬운 자리라 반드시 같이 본다.
    allowed = build_allowed_numbers(event)
    unknown = verify_numbers(
        " ".join(filter(None, [owner_sms, admin_note, emergency_report,
                               " ".join(next_steps), inquiry_draft])),
        allowed,
    )
    return NarrationResult(
        owner_sms=owner_sms,
        admin_note=admin_note,
        emergency_report=emergency_report,
        next_steps=next_steps,
        inquiry_draft=inquiry_draft,
        model=model,
        elapsed_ms=elapsed_ms,
        verification_passed=not unknown,
        unknown_numbers=unknown,
        raw_output=raw,
    )


def _narrate_with_model(event: dict, model: str, timeout: int) -> NarrationResult:
    raw, elapsed_ms = _call_llm(event, model, timeout)
    return _parse_and_verify(event, raw, model, elapsed_ms)


def narrate_event(event: dict, model: str | None = None) -> NarrationResult:
    """
    이벤트 dict -> 점주 문자/관리자 사유/(위험이면) 신고 초안/대처방안/문의 초안.

    event는 DB에서 조회한 값으로 채워야 한다(클라이언트가 보낸 수치를 그대로 쓰면
    임의의 값을 LLM에 먹일 수 있어 설명의 신뢰가 무너진다 - serving.get_anomaly_event 참고).

    1순위 모델 -> 대체 모델 순으로 시도한다. 호스팅 LLM은 예고 없이 응답 불능이 된다
    (실측: 1순위였던 mistral-nemotron이 2.8초에서 무응답으로 급변).

    **숫자 검증을 통과한 결과만 돌려준다.** 검증에 실패한 문장은 화면이 어차피 보여주지
    않는데, 그걸 저장하면 재생성 기능이 없는 이상 그 이벤트는 영영 빈칸이 된다. 그래서
    검증 실패도 호출 실패와 똑같이 다음 모델에게 넘긴다.

    예외: RuntimeError(API 키 없음) / NarrationUnavailable(모든 모델 실패).
    """
    if not settings.nvidia_api_key:
        raise RuntimeError("NVIDIA_API_KEY가 설정되지 않았습니다 (db/.env 확인).")

    candidates = [model] if model else [settings.nvidia_model, settings.nvidia_model_fallback]
    errors: list[str] = []
    deadline = time.time() + JOB_BUDGET_SEC
    for candidate in [c for c in candidates if c]:
        remaining = deadline - time.time()
        if remaining < MIN_ATTEMPT_SEC:
            errors.append(f"{candidate}: 예산소진({remaining:.1f}s 남음)")
            break
        try:
            result = _narrate_with_model(
                event, candidate, int(min(PER_MODEL_TIMEOUT_SEC, remaining))
            )
        except Exception as e:  # noqa: BLE001 - 어떤 실패든 다음 후보로 넘어간다
            # 예외 타입만 남기면 "왜 실패했는지"를 기록에서 알 수 없어 원인 추적이 막힌다
            # (실제로 모델 사망인지 파싱 실패인지 구분하는 데 시간을 썼다).
            # 메시지도 함께 남기되 혹시 섞여 들어올 수 있는 키 문자열은 지운다.
            detail = re.sub(r"nvapi-[A-Za-z0-9_\-]+", "***", str(e))[:120]
            errors.append(f"{candidate}: {type(e).__name__}({detail})")
            continue
        if not result.verification_passed:
            errors.append(f"{candidate}: 숫자검증실패({', '.join(result.unknown_numbers)})")
            continue
        return result

    raise NarrationUnavailable(f"설명문 생성 실패 - 시도: {' / '.join(errors)}")
