# -*- coding: utf-8 -*-
"""
data/프로젝트개요.md 개발우선순위 1번(영업유무·혼잡도)과 2번(안전감지 이상치 조회)에
대응하는 조회용 FastAPI. 포트 8000(Google Places 호출은 04단계 배치 스크립트 안에서
인프로세스로만 발생 - places-api-project는 더 이상 서버로 뜨지 않아 포트 충돌이 없음).

- 1번(영업유무/혼잡도): /api/stores*, /api/stores/{id}/status* 가 담당 - store_operating_status를
  09_compute_operating_status.py가 미리 계산해 둔 결과를 그대로 읽는다.
- 2번(안전감지): /api/anomalies가 담당 - anomaly_events(10_detect_anomalies.py 산출물)를 그대로 읽는다.
  단, "이상치 감지 결과 조회"까지만 커버한다. "점주에게 알림 문자 전송"(notified_at 채우기)은
  이번 범위 밖이라 이 API로는 할 수 없다.
- 3번(전력관리 효율화)은 팀 분석 보고서(data/03_지역특성_규모_업종_분석보고서.md 6장)에서
  이미 보류가 확정된 항목이라 이 API 범위 밖이고, 4번(SNS)도 아직 없다.

프론트엔드는 이번 작업 범위 밖("화면단에서 날짜 변경할 수 있다고 가정") -
이 엔드포인트가 그 가정을 충족시키는 백엔드 조회 로직이다.

Swagger UI: 서버 기동 후 http://localhost:8000/docs 에서 각 파라미터에 예시값이
채워진 채로 "Try it out"을 바로 눌러볼 수 있다.
"""
import sys
from datetime import date
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi import FastAPI, HTTPException, Path as PathParam, Query  # noqa: E402

from ami_db.db import get_engine  # noqa: E402
from ami_db.serving import (  # noqa: E402
    EARLIEST_SAMPLE_DATE,
    get_all_stores,
    get_anomalies,
    get_current_status_all,
    get_current_status_one,
    get_meter_day_series,
    get_store_day_series,
    get_store_hours,
    get_store_status_day,
)
from app.schemas import (  # noqa: E402
    AnomalyListResponse,
    CurrentStatusListResponse,
    CurrentStatusOneSchema,
    DayStatusResponse,
    MeterTimeseriesResponse,
    StoreHoursResponse,
    StoreListResponse,
    StoreTimeseriesResponse,
)

app = FastAPI(
    title="AMI 영업유무·혼잡도·안전감지 조회 API",
    description=(
        "화곡동 파일럿 21개 매장의 영업유무/혼잡도/이상치를 조회하는 API. "
        "데이터는 db/scripts/00~10 배치가 미리 계산해 PostgreSQL에 적재해 둔 것을 그대로 읽기만 한다. "
        f"조회 가능 날짜 범위: {EARLIEST_SAMPLE_DATE} ~ 오늘."
    ),
    version="0.1.0",
)
_engine = get_engine()

# Swagger 예시용으로 쓰는 실제 DB 값들 (store_id는 항상 1~21, meter_id는 A-L-nn 패턴).
# 자세한 매핑은 db/docs/API_REFERENCE.md 참고.
EXAMPLE_STORE_ID = 1          # 못난이찹쌀꽈배기 - Google에 운영시간 정보 없어 ksic_estimate 폴백 케이스
EXAMPLE_METER_ID = "A-L-11"   # 위 store_id=1과 동일 매장의 계기번호
EXAMPLE_DATE_REAL = date(2026, 5, 15)       # 실측 구간 예시 날짜
EXAMPLE_DATE_SYNTHETIC = date(2026, 8, 15)  # 합성 구간 예시 날짜
EXAMPLE_ANOMALY_METER_ID = "A-L-71"         # 실제로 '위험' 이벤트가 존재하는 계기


@app.get("/health", tags=["기본"], summary="헬스체크")
def health():
    return {"status": "ok"}


@app.get(
    "/api/stores", tags=["매장정보"], summary="매장 목록",
    response_model=StoreListResponse,
)
def list_stores():
    """화곡동에 매칭된 21개 매장의 기본정보(이름/주소/좌표/업종) 전체. 파라미터 없음 - 바로 실행."""
    return {"stores": get_all_stores(_engine)}


@app.get(
    "/api/stores/status", tags=["영업유무·혼잡도"], summary="전체 매장 현재 상태 (메인 화면용)",
    response_model=CurrentStatusListResponse,
)
def list_current_status():
    """
    21개 매장 전부의 **지금 이 순간** 영업유무(final_status)와 혼잡도(congestion_level)를
    한 번에 반환한다. 파라미터 없음 - 바로 실행. 지도/목록 메인 화면이 이 엔드포인트
    하나만 호출하면 마커 색칠까지 끝난다(final_status로 아이콘, congestion_level로 색상).
    """
    return {"stores": get_current_status_all(_engine)}


@app.get(
    "/api/stores/{store_id}/status/current", tags=["영업유무·혼잡도"], summary="매장 1곳 현재 상태",
    response_model=CurrentStatusOneSchema,
)
def store_current_status(
    store_id: int = PathParam(..., description="상가 ID (1~21)", examples=[EXAMPLE_STORE_ID]),
):
    """
    예시: `/api/stores/1/status/current` -> 못난이찹쌀꽈배기의 현재 상태.
    store_id 범위를 벗어나거나(1~21이 아니거나) 아직 상태가 계산되지 않았으면 404.
    """
    result = get_current_status_one(_engine, store_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"store_id={store_id}의 상태 데이터가 없습니다 (1~21 범위인지 확인하세요)")
    return result


@app.get(
    "/api/stores/{store_id}/hours", tags=["영업유무·혼잡도"], summary="매장 1곳의 요일별 운영시간",
    response_model=StoreHoursResponse,
)
def store_hours(
    store_id: int = PathParam(..., description="상가 ID (1~21)", examples=[EXAMPLE_STORE_ID]),
):
    """
    예시: `/api/stores/1/hours` -> 못난이찹쌀꽈배기의 요일별(월~일) 운영시간.
    google_places 실측이 있으면 그걸, 없으면 ksic_estimate(업종코드 기반 추정)를
    반환한다 - 각 행의 `source`로 어느 쪽인지 구분된다. store_id 범위를 벗어나면 404.
    """
    rows = get_store_hours(_engine, store_id)
    if rows is None:
        raise HTTPException(status_code=404, detail=f"store_id={store_id}의 매장이 없습니다 (1~21 범위인지 확인하세요)")
    return {"store_id": store_id, "hours": rows}


@app.get(
    "/api/stores/{store_id}/status", tags=["영업유무·혼잡도"], summary="매장 1곳의 하루 상태+전력 타임라인",
    response_model=DayStatusResponse,
)
def store_status_day(
    store_id: int = PathParam(..., description="상가 ID (1~21)", examples=[EXAMPLE_STORE_ID]),
    date: date = Query(
        ...,
        description=f"조회할 날짜. {EARLIEST_SAMPLE_DATE}~2026-06-30은 실측, "
                     f"2026-07-01~오늘은 합성 데이터(is_synthetic로 구분됨).",
        examples=[EXAMPLE_DATE_REAL, EXAMPLE_DATE_SYNTHETIC],
    ),
):
    """
    예시: `/api/stores/1/status?date=2026-08-15` -> 96개(15분×24시간) 슬롯의
    schedule_status/power_status/final_status/congestion_level + 실제 전력값(kWh).
    data/images/user-메인-*.png의 "오늘 시간대별" 차트를 이 한 번의 호출로 그릴 수 있다.
    """
    try:
        result = get_store_status_day(_engine, store_id, date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "store_id": store_id,
        "date": date.isoformat(),
        "data_resolution": result.data_resolution,
        "rows": result.rows,
    }


@app.get(
    "/api/anomalies", tags=["안전감지"], summary="이상치 이벤트 목록 (관리자 화면용)",
    response_model=AnomalyListResponse,
)
def list_anomalies(
    level: Literal["주의", "위험"] | None = Query(
        default=None, description="심각도 필터. 비우면 전체.", examples=["위험"],
    ),
    meter_id: str | None = Query(
        default=None, description="특정 계기만 조회하고 싶을 때.", examples=[EXAMPLE_ANOMALY_METER_ID],
    ),
    since: date | None = Query(default=None, description="이 날짜 이후(포함)만 조회.", examples=["2026-08-01"]),
    until: date | None = Query(default=None, description="이 날짜 이전(포함)까지만 조회.", examples=["2026-09-01"]),
    limit: int = Query(default=200, ge=1, le=2000, description="최대 반환 건수(최신순).", examples=[50]),
):
    """
    예시: `/api/anomalies?level=위험&limit=10` -> 위험 등급 최신 10건.
    파라미터를 하나도 안 주면(`/api/anomalies`) 전체 계기의 최신 이상치 200건이 반환된다.
    각 행에 매장명(store_name)까지 조인되어 있어 계기번호를 몰라도 바로 알아볼 수 있다.
    안전감지는 "영업종료 이후"에만 판정한다(영업시간 중 스파이크는 혼잡도 문제일 뿐 안전 이슈가 아니라고
    봄 - data/프로젝트개요.md 안전감지 항목 참고).
    """
    return {"anomalies": get_anomalies(_engine, level, meter_id, since, until, limit)}


@app.get(
    "/api/meters/{meter_id}/timeseries", tags=["원시 전력값"], summary="계기 1곳의 하루 원시 전력값",
    response_model=MeterTimeseriesResponse,
)
def meter_timeseries(
    meter_id: str = PathParam(..., description="계기번호(예: 'A-L-11')", examples=[EXAMPLE_METER_ID]),
    date: date = Query(..., description="조회할 날짜", examples=[EXAMPLE_DATE_REAL, EXAMPLE_DATE_SYNTHETIC]),
):
    """
    상태 판정 없이 15분 단위 전력값(kWh)만 필요할 때 쓴다. 상태+전력을 같이 보려면
    `/api/stores/{store_id}/status`를 대신 쓰는 게 낫다(이 엔드포인트는 순수 원시값용).
    """
    try:
        result = get_meter_day_series(_engine, meter_id, date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "meter_id": result.meter_id,
        "date": result.target_date.isoformat(),
        "is_synthetic": result.is_synthetic,
        "data_resolution": result.data_resolution,
        "rows": result.rows,
    }


@app.get(
    "/api/stores/{store_id}/timeseries", tags=["원시 전력값"], summary="매장 1곳의 하루 원시 전력값",
    response_model=StoreTimeseriesResponse,
)
def store_timeseries(
    store_id: int = PathParam(..., description="상가 ID (1~21)", examples=[EXAMPLE_STORE_ID]),
    date: date = Query(..., description="조회할 날짜", examples=[EXAMPLE_DATE_REAL, EXAMPLE_DATE_SYNTHETIC]),
):
    """meter_timeseries와 동일하나 store_id(상가 기준)로 조회한다."""
    try:
        result = get_store_day_series(_engine, store_id, date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "store_id": store_id,
        "meter_id": result.meter_id,
        "date": result.target_date.isoformat(),
        "is_synthetic": result.is_synthetic,
        "data_resolution": result.data_resolution,
        "rows": result.rows,
    }
