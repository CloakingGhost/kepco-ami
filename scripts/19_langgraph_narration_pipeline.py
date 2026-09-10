# -*- coding: utf-8 -*-
"""
설명 계층(ami_db.narrate)의 AI 파이프라인을 LangGraph StateGraph로 재구성.

**목적은 재구현이 아니라 시각화다.** narrate.narrate_event()가 실제로 하는 분기
(캐시 우선 조회 -> API 키 확인 -> 1순위 모델 -> 실패 시 대체 모델 -> 그것도 실패하면
캐시 안전망 -> 파싱 -> 숫자 검증 -> 캐시 저장)를 노드/조건부 엣지로 그대로 옮기고,
narrate.py의 실제 내부 함수(_call_llm, _extract_json, build_allowed_numbers,
verify_numbers, _load_cache, _save_cache)를 노드 안에서 그대로 호출한다 - 로직을
복제하면 두 구현이 갈라질 수 있으므로 절대 재작성하지 않는다.

이 그래프를 만든 이유 둘:
  1. `compiled.get_graph().draw_mermaid()`로 파이프라인 구조를 언제든 다시 뽑을 수 있다
     (손으로 그린 다이어그램은 코드가 바뀌면 조용히 낡는다 - 이건 코드에서 직접 나온다).
  2. `graph.invoke(...)`로 실제 실행도 된다 - narrate_event()의 대안 실행 경로로 쓰거나,
     향후 추적 에이전트(안전감지_이상치_판정기준.md 7절)로 확장할 때 이 그래프에
     노드를 추가하는 형태로 시작할 수 있다.

**주의**: 운영 서버(app/serving_api.py)는 narrate.narrate_event()를 직접 호출하고
이 파일에는 의존하지 않는다. langgraph는 dev 전용 의존성(pyproject.toml)이라
배포에는 영향이 없다.

사용법:
    uv sync --extra dev                              # langgraph 설치(최초 1회)
    uv run python scripts/19_langgraph_narration_pipeline.py            # 그래프만 출력
    uv run python scripts/19_langgraph_narration_pipeline.py --run      # 실제 DB 이벤트로 실행까지
    uv run python scripts/19_langgraph_narration_pipeline.py --run --prefer-cache
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from langgraph.graph import END, START, StateGraph  # noqa: E402

from ami_db import narrate  # noqa: E402
from ami_db.config import settings  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[1] / "output" / "generated"


# ============================================================
# 상태 정의 - narrate_event()가 지역변수로 들고 있던 값을 그래프 상태로 노출한 것뿐이다.
# ============================================================

class PipelineState(TypedDict, total=False):
    event: dict                    # DB에서 읽은 이벤트(get_anomaly_event 결과)
    prefer_cache: bool             # 시연용: 캐시 있으면 LLM 스킵

    cache_key: str
    cached: dict | None

    model: str                     # 이번 시도에 쓸 모델(1순위/대체)
    timeout: int
    attempt_label: str             # 그래프 실행 로그용("primary" | "fallback")
    raw_output: str
    call_error: str | None         # 이번 모델 호출이 실패했으면 에러 메시지

    owner_sms: str
    admin_note: str
    emergency_report: str | None

    allowed_numbers: set
    unknown_numbers: list
    verification_passed: bool

    source: Literal["live", "cache"]
    final: dict | None             # 성공 결과(narrate.NarrationResult에 대응하는 dict)
    error: str | None              # 최종 실패 사유(503/502에 대응)


# ============================================================
# 노드 - 각 함수는 narrate.py의 실제 내부 함수를 그대로 호출한다(재구현 금지).
# ============================================================

def check_cache(state: PipelineState) -> PipelineState:
    key = narrate._cache_key(state["event"])
    return {"cache_key": key, "cached": narrate._load_cache().get(key)}


def check_api_key(state: PipelineState) -> PipelineState:
    return {}  # 라우팅만 하는 노드 - route_after_key_check가 settings를 직접 본다


def build_payload(state: PipelineState) -> PipelineState:
    narrate.build_prompt_payload(state["event"])  # 실제 변환 로직 실행(검증 목적)
    return {"model": settings.nvidia_model, "timeout": narrate.PER_MODEL_TIMEOUT_SEC,
            "attempt_label": "primary"}


def _call_model_node(state: PipelineState) -> PipelineState:
    """call_primary/call_fallback 공용 본체. _call_llm()을 그대로 호출한다."""
    try:
        raw, elapsed_ms = narrate._call_llm(state["event"], state["model"], state["timeout"])
        return {"raw_output": raw, "call_error": None, "_elapsed_ms": elapsed_ms}
    except Exception as e:  # noqa: BLE001 - narrate_event()와 동일하게 어떤 실패든 다음 단계로
        return {"raw_output": "", "call_error": f"{state['attempt_label']}({state['model']}): "
                                                 f"{type(e).__name__}: {e}"}


def call_primary(state: PipelineState) -> PipelineState:
    return _call_model_node(state)


def call_fallback(state: PipelineState) -> PipelineState:
    return _call_model_node({
        **state, "model": settings.nvidia_model_fallback,
        "timeout": narrate.PER_MODEL_TIMEOUT_SEC, "attempt_label": "fallback",
    })


def parse_response(state: PipelineState) -> PipelineState:
    parsed = narrate._extract_json(state["raw_output"])
    owner_sms = str(parsed.get("owner_sms", "")).strip()
    admin_note = str(parsed.get("admin_note", "")).strip()
    if not owner_sms or not admin_note:
        raise ValueError(f"필수 필드가 비어 있습니다: {state['raw_output'][:200]}")
    emergency = parsed.get("emergency_report")
    return {
        "owner_sms": owner_sms, "admin_note": admin_note,
        "emergency_report": str(emergency).strip() if emergency else None,
    }


def verify(state: PipelineState) -> PipelineState:
    allowed = narrate.build_allowed_numbers(state["event"])
    text = " ".join(filter(None, [
        state["owner_sms"], state["admin_note"], state.get("emergency_report"),
    ]))
    unknown = narrate.verify_numbers(text, allowed)
    return {"unknown_numbers": unknown, "verification_passed": not unknown}


def save_and_finish(state: PipelineState) -> PipelineState:
    result = narrate.NarrationResult(
        owner_sms=state["owner_sms"], admin_note=state["admin_note"],
        emergency_report=state.get("emergency_report"), model=state["model"],
        elapsed_ms=state.get("_elapsed_ms", 0),
        verification_passed=state["verification_passed"],
        unknown_numbers=state["unknown_numbers"], source="live",
    )
    narrate._save_cache(state["cache_key"], result)
    return {"source": "live", "final": {
        "owner_sms": result.owner_sms, "admin_note": result.admin_note,
        "emergency_report": result.emergency_report, "model": result.model,
        "verification_passed": result.verification_passed,
        "unknown_numbers": result.unknown_numbers, "source": "live",
    }}


def return_cached(state: PipelineState) -> PipelineState:
    cached = state.get("cached") or narrate._load_cache().get(state["cache_key"])
    return {"source": "cache", "final": {**cached, "source": "cache"}}


def build_error(state: PipelineState) -> PipelineState:
    if not settings.nvidia_api_key:
        return {"error": "NVIDIA_API_KEY가 설정되지 않았습니다 (503에 대응)"}
    return {"error": f"설명문 생성 실패, 캐시도 없음 (502에 대응) - {state.get('call_error')}"}


# ============================================================
# 조건부 라우팅 - narrate_event()의 if/for 분기를 그대로 옮긴 것.
# ============================================================

def route_after_cache_check(state: PipelineState) -> str:
    if state["prefer_cache"] and state.get("cached"):
        return "hit"
    return "miss"


def route_after_key_check(state: PipelineState) -> str:
    return "has_key" if settings.nvidia_api_key else "no_key"


def route_after_call(state: PipelineState) -> str:
    return "ok" if not state.get("call_error") else "fail"


def route_after_fallback(state: PipelineState) -> str:
    if not state.get("call_error"):
        return "ok"
    cached = narrate._load_cache().get(state["cache_key"])
    return "cache_hit" if cached else "no_cache"


# ============================================================
# 그래프 조립
# ============================================================

def build_graph():
    g = StateGraph(PipelineState)

    g.add_node("check_cache", check_cache)
    g.add_node("check_api_key", check_api_key)
    g.add_node("build_payload", build_payload)
    g.add_node("call_primary", call_primary)
    g.add_node("call_fallback", call_fallback)
    g.add_node("parse_response", parse_response)
    g.add_node("verify_numbers", verify)
    g.add_node("save_and_finish", save_and_finish)
    g.add_node("return_cached", return_cached)
    g.add_node("build_error", build_error)

    g.add_edge(START, "check_cache")
    g.add_conditional_edges("check_cache", route_after_cache_check,
                             {"hit": "return_cached", "miss": "check_api_key"})
    g.add_conditional_edges("check_api_key", route_after_key_check,
                             {"has_key": "build_payload", "no_key": "build_error"})
    g.add_edge("build_payload", "call_primary")
    g.add_conditional_edges("call_primary", route_after_call,
                             {"ok": "parse_response", "fail": "call_fallback"})
    g.add_conditional_edges("call_fallback", route_after_fallback,
                             {"ok": "parse_response", "cache_hit": "return_cached",
                              "no_cache": "build_error"})
    g.add_edge("parse_response", "verify_numbers")
    g.add_edge("verify_numbers", "save_and_finish")
    g.add_edge("save_and_finish", END)
    g.add_edge("return_cached", END)
    g.add_edge("build_error", END)

    return g.compile()


# ============================================================
# 실행 진입점
# ============================================================

# DB가 꺼져 있어도 이 스크립트만 따로 돌려볼 수 있도록 남겨두는 폴백값.
# 2026-09-10 프로덕션 실호출로 검증된 실제 값(docs/LLM_설명API_검증결과.md 3-1절)과 동일하다 -
# 임의로 지어낸 값이 아니다.
_OFFLINE_DEMO_EVENT = {
    "store_id": 12, "store_name": "충북식당", "biz_category_mid": "한식",
    "contract_power_kw": 45.0, "level": "위험",
    "rule_triggered": "kec212_overload_130pct_60min",
    "detected_at": datetime(2026, 7, 2, 2, 15),
    "metric_value": 18.94857157639569, "threshold_value": 14.625,
}


def _fetch_demo_event() -> dict:
    """
    실제 DB의 위험 데모 이벤트(A-L-60 충북식당)를 가져온다. DB가 꺼져 있으면(로컬
    Docker 미기동 등) 같은 이벤트의 고정값으로 대신한다 - 그래프 실행 자체를 보는 게
    목적일 땐 DB가 필수 전제가 아니어야 "별도로" 쓰기 편하다.
    """
    try:
        from ami_db.db import get_engine
        from ami_db.serving import get_anomaly_event

        event = get_anomaly_event(
            get_engine(), store_id=12, detected_at=datetime(2026, 7, 2, 2, 15)
        )
        if event is not None:
            return event
        print("[안내] DB는 연결됐지만 해당 이벤트가 없습니다 - 오프라인 고정값으로 대체합니다.")
    except Exception as e:  # noqa: BLE001 - DB 연결 실패는 이 스크립트의 목적을 막지 않는다
        print(f"[안내] DB 연결 실패({type(e).__name__}) - 오프라인 고정값으로 대체합니다.")
    return dict(_OFFLINE_DEMO_EVENT)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", help="그래프 구조만 보지 않고 실제로 실행")
    parser.add_argument("--prefer-cache", action="store_true", help="--run과 함께: 캐시 우선")
    args = parser.parse_args()

    compiled = build_graph()

    mermaid = compiled.get_graph().draw_mermaid()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    mmd_path = OUT_DIR / "narration_pipeline.mmd"
    mmd_path.write_text(mermaid, encoding="utf-8")
    print(f"[1/2] Mermaid 저장: {mmd_path}\n")
    print(mermaid)

    try:
        png = compiled.get_graph().draw_mermaid_png()
        png_path = OUT_DIR / "narration_pipeline.png"
        png_path.write_bytes(png)
        print(f"\n[2/2] PNG 저장: {png_path}")
    except Exception as e:  # noqa: BLE001 - mermaid.ink 호출(네트워크) 실패는 치명적이지 않음
        print(f"\n[2/2] PNG 렌더링 생략(네트워크 필요, mermaid.ink 호출): {type(e).__name__}")

    if not args.run:
        print("\n(그래프 구조만 출력했습니다. 실제 DB 이벤트로 실행하려면 --run)")
        return

    print("\n" + "=" * 60 + "\n실행: 실제 DB 이벤트(A-L-60 충북식당, 2026-07-02 02:15)\n")
    event = _fetch_demo_event()
    initial: PipelineState = {"event": event, "prefer_cache": args.prefer_cache}
    final_state = compiled.invoke(initial)

    result = final_state.get("final")
    if result:
        print(f"source={result['source']} | model={result.get('model', '-')}")
        print(f"검증={result['verification_passed']} {result.get('unknown_numbers', [])}")
        print(f"점주: {result['owner_sms']}")
        print(f"관리자: {result['admin_note']}")
    else:
        print(f"실패: {final_state.get('error')}")


if __name__ == "__main__":
    main()
