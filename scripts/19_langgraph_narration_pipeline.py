# -*- coding: utf-8 -*-
"""
AI 분석 파이프라인을 LangGraph StateGraph로 재구성 - **목적은 재구현이 아니라 시각화다.**

실제 서비스(app/serving_api.py)가 하는 분기를 노드/조건부 엣지로 그대로 옮기고, 노드 안에서는
narrate.py·serving.py의 실제 함수를 그대로 호출한다 - 로직을 복제하면 두 구현이 갈라질 수
있으므로 절대 재작성하지 않는다.

흐름(2026-09-10 개편 - 요청했을 때만 생성하고 DB에 저장):
  DB 조회 -> 완료본이 있으면 그대로 반환(AI 호출 없음)
          -> 없거나 실패였으면: 키 확인 -> 작업 선점(claim) -> 1순위 모델 -> 파싱·숫자 검증
             -> 통과면 DB에 저장(done) / 실패면 대체 모델 -> 그것도 실패면 DB에 failed 기록
          -> 이미 다른 요청이 생성 중이면 '분석 중' 반환

실서비스에서는 claim_job 지점에서 요청이 둘로 갈린다. 요청 핸들러는 거기서 'pending'을
응답하고, 그 뒤(call_primary 이하)는 백그라운드 작업이 이어서 한다 - LLM이 24~57초라
요청을 붙잡고 기다리면 앞단 프록시(30초)가 먼저 끊기 때문이다. 이 그래프는 두 부분을
한 줄로 이어 그린 것이다.

이전 버전과 달라진 점: 파일 캐시(사전 생성본) 노드가 없다. 요청하지 않은 분석을 미리
만들어 두는 건 자원 낭비이고, "저장된 분석"은 누군가 실제로 요청해 AI가 만든 결과여야 한다.

이 그래프를 둔 이유:
  1. `compiled.get_graph().draw_mermaid()`로 파이프라인 구조를 언제든 다시 뽑을 수 있다
     (손으로 그린 다이어그램은 코드가 바뀌면 조용히 낡는다 - 이건 코드에서 직접 나온다).
  2. `graph.invoke(...)`로 실제 실행도 된다.

**주의**: 운영 서버는 이 파일에 의존하지 않는다. langgraph는 dev 전용 의존성(pyproject.toml)이다.

사용법:
    uv sync --extra dev                                             # langgraph 설치(최초 1회)
    uv run python scripts/19_langgraph_narration_pipeline.py        # 그래프만 출력(DB 불필요)
    uv run python scripts/19_langgraph_narration_pipeline.py --run  # 실제 DB 이벤트로 실행
        --run은 anomaly_narrations에 실제로 기록한다(화면에서 요청한 것과 똑같이 저장된다).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from langgraph.graph import END, START, StateGraph  # noqa: E402

from ami_db import narrate  # noqa: E402
from ami_db.config import settings  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[1] / "output" / "generated"


# ============================================================
# 상태 정의 - 서비스 코드가 지역변수로 들고 있던 값을 그래프 상태로 노출한 것뿐이다.
# ============================================================

class PipelineState(TypedDict, total=False):
    engine: Any                    # SQLAlchemy Engine (--run일 때만)
    event: dict                    # DB에서 읽은 이벤트(get_anomaly_event 결과)
    stored: dict | None            # anomaly_narrations 행(get_narration 결과)
    claimed: bool                  # 이 실행이 생성 작업을 맡았는가

    model: str                     # 이번 시도에 쓴 모델
    attempt_label: str             # "primary" | "fallback"
    raw_output: str
    elapsed_ms: int
    attempt_error: str | None      # 이번 시도의 실패 사유(호출·파싱·숫자검증)
    result: Any                    # narrate.NarrationResult (검증 통과본)

    outcome: str                   # 최종 상태: done | pending | failed
    detail: str                    # 실행 로그용 설명


def _serving():
    # serving은 pandas/numpy를 끌어온다. 그래프 구조만 뽑을 땐 필요 없어서 늦게 import한다.
    from ami_db import serving
    return serving


# ============================================================
# 노드 - 각 함수는 실제 서비스 함수를 그대로 호출한다(재구현 금지).
# ============================================================

def check_db(state: PipelineState) -> PipelineState:
    return {"stored": _serving().get_narration(state["engine"], state["event"]["event_id"])}


def check_api_key(state: PipelineState) -> PipelineState:
    return {}  # 라우팅만 하는 노드 - route_after_key_check가 settings를 직접 본다


def claim_job(state: PipelineState) -> PipelineState:
    return {"claimed": _serving().claim_narration(state["engine"], state["event"]["event_id"])}


def _attempt(state: PipelineState, model: str, label: str) -> PipelineState:
    """call_primary/call_fallback 공용 본체. narrate._call_llm()을 그대로 호출한다."""
    try:
        raw, elapsed_ms = narrate._call_llm(state["event"], model, narrate.PER_MODEL_TIMEOUT_SEC)
    except Exception as e:  # noqa: BLE001 - 서비스와 동일하게 어떤 실패든 다음 단계로
        return {"model": model, "attempt_label": label, "raw_output": "",
                "attempt_error": f"호출 실패 {type(e).__name__}"}
    return {"model": model, "attempt_label": label, "raw_output": raw,
            "elapsed_ms": elapsed_ms, "attempt_error": None}


def call_primary(state: PipelineState) -> PipelineState:
    return _attempt(state, settings.nvidia_model, "primary")


def call_fallback(state: PipelineState) -> PipelineState:
    return _attempt(state, settings.nvidia_model_fallback, "fallback")


def parse_and_verify(state: PipelineState) -> PipelineState:
    """narrate._parse_and_verify()를 그대로 부른다. 검증 실패도 '이번 시도 실패'로 친다."""
    if state.get("attempt_error"):
        return {}
    try:
        result = narrate._parse_and_verify(
            state["event"], state["raw_output"], state["model"], state.get("elapsed_ms", 0)
        )
    except Exception as e:  # noqa: BLE001
        return {"attempt_error": f"파싱 실패 {type(e).__name__}"}
    if not result.verification_passed:
        return {"attempt_error": f"숫자검증 실패 {result.unknown_numbers}"}
    return {"result": result}


def save_done(state: PipelineState) -> PipelineState:
    _serving().save_narration_done(state["engine"], state["event"]["event_id"], state["result"])
    return {"outcome": "done", "detail": f"{state['model']}로 생성해 DB에 저장"}


def save_failed(state: PipelineState) -> PipelineState:
    reason = state.get("attempt_error") or "알 수 없음"
    _serving().save_narration_failed(state["engine"], state["event"]["event_id"], reason)
    return {"outcome": "failed", "detail": f"모든 모델 실패 - {reason}"}


def return_stored(state: PipelineState) -> PipelineState:
    return {"outcome": "done", "detail": "DB 저장본 반환(AI 호출 없음)"}


def return_pending(state: PipelineState) -> PipelineState:
    return {"outcome": "pending", "detail": "다른 요청이 이미 생성 중"}


def report_unavailable(state: PipelineState) -> PipelineState:
    return {"outcome": "failed", "detail": "API 키 없음 - DB에 기록하지 않고 안내만 반환"}


# ============================================================
# 조건부 라우팅 - serving_api.request_anomaly_analysis()와 narrate_event()의 분기 그대로.
# ============================================================

def route_after_db(state: PipelineState) -> str:
    stored = state.get("stored")
    return "stored" if stored and stored["status"] == "done" else "generate"


def route_after_key_check(state: PipelineState) -> str:
    return "has_key" if settings.nvidia_api_key else "no_key"


def route_after_claim(state: PipelineState) -> str:
    return "claimed" if state.get("claimed") else "in_progress"


def route_after_attempt(state: PipelineState) -> str:
    if not state.get("attempt_error"):
        return "ok"
    if state.get("attempt_label") == "primary" and settings.nvidia_model_fallback:
        return "retry"
    return "give_up"


# ============================================================
# 그래프 조립
# ============================================================

def build_graph():
    g = StateGraph(PipelineState)

    g.add_node("check_db", check_db)
    g.add_node("check_api_key", check_api_key)
    g.add_node("claim_job", claim_job)
    g.add_node("call_primary", call_primary)
    g.add_node("call_fallback", call_fallback)
    g.add_node("parse_and_verify", parse_and_verify)
    g.add_node("save_done", save_done)
    g.add_node("save_failed", save_failed)
    g.add_node("return_stored", return_stored)
    g.add_node("return_pending", return_pending)
    g.add_node("report_unavailable", report_unavailable)

    g.add_edge(START, "check_db")
    g.add_conditional_edges("check_db", route_after_db,
                            {"stored": "return_stored", "generate": "check_api_key"})
    g.add_conditional_edges("check_api_key", route_after_key_check,
                            {"has_key": "claim_job", "no_key": "report_unavailable"})
    g.add_conditional_edges("claim_job", route_after_claim,
                            {"claimed": "call_primary", "in_progress": "return_pending"})
    g.add_edge("call_primary", "parse_and_verify")
    g.add_edge("call_fallback", "parse_and_verify")
    g.add_conditional_edges("parse_and_verify", route_after_attempt,
                            {"ok": "save_done", "retry": "call_fallback", "give_up": "save_failed"})
    for terminal in ("save_done", "save_failed", "return_stored", "return_pending",
                     "report_unavailable"):
        g.add_edge(terminal, END)

    return g.compile()


# ============================================================
# 실행 진입점
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true",
                        help="그래프 구조만 보지 않고 실제 DB 이벤트로 실행(anomaly_narrations에 기록됨)")
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

    # --run은 DB가 필요하다 - 분석을 DB에서 읽고 DB에 저장하는 것 자체가 이 파이프라인이다.
    try:
        from ami_db.db import get_engine
        from ami_db.serving import get_anomaly_event

        engine = get_engine()
        event = get_anomaly_event(engine, store_id=12, detected_at=datetime(2026, 7, 2, 2, 15))
    except Exception as e:  # noqa: BLE001
        print(f"\n[실행 불가] DB 연결 실패({type(e).__name__}) - Postgres 컨테이너를 켜고 다시 실행하세요.")
        return
    if event is None:
        print("\n[실행 불가] DB에 데모 이벤트(A-L-60 충북식당 2026-07-02 02:15)가 없습니다.")
        return

    print("\n" + "=" * 60 + "\n실행: 실제 DB 이벤트(A-L-60 충북식당, 2026-07-02 02:15)\n")
    final = compiled.invoke({"engine": engine, "event": event})
    print(f"결과: {final.get('outcome')} - {final.get('detail')}")
    stored = _serving().get_narration(engine, event["event_id"])
    if stored and stored["status"] == "done":
        print(f"점주: {stored['owner_sms']}")
        print(f"관리자: {stored['admin_note']}")


if __name__ == "__main__":
    main()
