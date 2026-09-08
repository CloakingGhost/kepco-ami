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
    LATEST_SERVICE_DATE,
    LATEST_SNAPSHOT_DATE,
    get_all_stores,
    get_anomalies,
    get_current_status_all,
    get_current_status_one,
    get_meter_day_series,
    get_store_day_series,
    get_store_detail,
    get_store_hours,
    get_store_status_day,
    get_stores_snapshot,
    parse_snapshot_time,
)
from app.schemas import (  # noqa: E402
    AnomalyListResponse,
    CurrentStatusListResponse,
    CurrentStatusOneSchema,
    DayStatusResponse,
    MeterTimeseriesResponse,
    StoreDetailRequest,
    StoreDetailResponse,
    StoreHoursResponse,
    StoreListResponse,
    StoreSnapshotResponse,
    StoreTimeseriesResponse,
)

app = FastAPI(
    title="AMI 영업유무·혼잡도·안전감지 조회 API",
    description=(
        "화곡동 파일럿 21개 매장의 영업유무/혼잡도/이상치를 조회하는 API. "
        "데이터는 db/scripts/00~10 배치가 미리 계산해 PostgreSQL에 적재해 둔 것을 그대로 읽기만 한다. "
        f"조회 가능 날짜 범위: {EARLIEST_SAMPLE_DATE} ~ {LATEST_SERVICE_DATE} "
        f"(2026-06-30까지는 실측, 7월은 안전감지 데모용 합성 구간)."
    ),
    version="0.1.0",
)
_engine = get_engine()

# Swagger 예시용으로 쓰는 실제 DB 값들 (store_id는 항상 1~21, meter_id는 A-L-nn 패턴).
# 자세한 매핑은 db/docs/API_REFERENCE.md 참고.
EXAMPLE_STORE_ID = 1          # 못난이찹쌀꽈배기 - Google에 운영시간 정보 없어 ksic_estimate 폴백 케이스
EXAMPLE_METER_ID = "A-L-11"   # 위 store_id=1과 동일 매장의 계기번호
EXAMPLE_DATE_REAL = date(2026, 5, 15)       # 실측 구간 예시 날짜
EXAMPLE_DATE_SYNTHETIC = date(2026, 7, 15)  # 합성 구간 예시 날짜(7월, 주의 시나리오가 심긴 날)
EXAMPLE_DANGER_METER_ID = "A-L-60"          # 7월 '위험' 시나리오가 심긴 계기(충북식당)
EXAMPLE_CAUTION_METER_ID = "A-L-65"         # 7월 '주의' 시나리오가 심긴 계기(와카츠)


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


@app.post(
    "/api/stores", tags=["매장정보"], summary="매장 1곳 상세 조회",
    response_model=StoreDetailResponse,
)
def store_detail(body: StoreDetailRequest):
    """
    store_id를 body로 받는다(GET 경로에 store_id를 노출하지 않으려고 POST를 씀 - 목록
    조회용 GET /api/stores와 경로는 같지만 메서드가 달라 공존한다).

    평점/전화번호/웹사이트(Google Places, google_places_cache 최신 행) + 요일별(월~일)
    영업시간 + 지금 이 순간의 영업상태/혼잡도를 한 번에 반환한다. Google Places 정보나
    현재 상태 데이터가 없으면 해당 필드는 null이 되고 message에 안내 문구가 채워진다
    (매장 자체는 존재하므로 404가 아니라 200으로 응답).

    final_status는 내부 4값 중 '예외영업'을 '영업종료'로 접어 영업중/휴무추정/영업종료
    3값으로만 내려준다.

    **기준 시각**: body에 date/time을 같이 주면 그 시점 기준으로 영업상태를 판정한다.
    목록(`/api/stores/snapshot`)에서 사용자가 고른 날짜·시각을 그대로 넘기면 목록과
    상세가 항상 같은 상태를 보여준다(안 넘기면 서버의 현재 시각 기준이라 목록이
    과거 시각을 보고 있을 때 둘이 어긋난다).

    store_id가 1~21 범위를 벗어나면 404, date/time 형식이 잘못되면 400.
    """
    try:
        result = get_store_detail(_engine, body.store_id, body.date, body.time)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result is None:
        raise HTTPException(
            status_code=404, detail=f"store_id={body.store_id}의 매장이 없습니다 (1~21 범위인지 확인하세요)"
        )
    return result


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
    "/api/stores/snapshot", tags=["영업유무·혼잡도"], summary="특정 날짜·시각의 전체 매장 스냅샷",
    response_model=StoreSnapshotResponse,
)
def stores_snapshot(
    date: str = Query(
        default="26-05-09",
        description=f"조회할 날짜, 'YY-MM-DD' 형식(연도 2자리). 범위: "
                    f"{EARLIEST_SAMPLE_DATE.strftime('%y-%m-%d')} ~ {LATEST_SNAPSHOT_DATE.strftime('%y-%m-%d')} "
                    f"(AMI 샘플데이터 실측 구간).",
    ),
    time: str = Query(
        default="19:15",
        description="조회할 시각, 'HH:MM' 형식(00:00~23:45, 15분 단위만 허용: 00/15/30/45).",
    ),
):
    """
    예시: `/api/stores/snapshot?date=26-05-09&time=19:15` -> 그 시점 21개 매장의
    위치/영업상태/혼잡도/전력사용량을 한 번에 반환한다.

    조회 가능 날짜 범위는 AMI 샘플데이터의 시작일~마지막일인 2026-04-01~2026-06-30로
    고정된다(다른 엔드포인트처럼 오늘까지의 합성 구간을 포함하지 않음).

    계기 해상도 처리: data_resolution='1hour'인 매장(5개)은 15/30/45분 슬롯이 애초에
    없으므로 입력 시각의 "시"만 사용해 정각 데이터를 가져온다(예: 19:15 입력 -> 19:00 슬롯).
    나머지 '15min' 매장은 입력 시각을 그대로 사용한다. 매장별로 그 시점 데이터 자체가
    없으면(정각 슬롯 결측 등) 상태 관련 필드가 전부 null이 되고 message에 안내 문구가 채워진다.

    final_status는 내부 4값 중 '예외영업'을 '영업종료'로 접어 영업중/휴무추정/영업종료
    3값으로만 내려준다(일반 사용자는 영업 중인지 아닌지만 판단하면 되기 때문).

    congestion_level은 문자열이 아니라 정수 코드로 내려온다: 0=해당없음(영업중이
    아니거나 데이터 없음) | 1=하 | 2=중 | 3=상.

    date/time 형식이 잘못됐거나 date가 조회 가능 범위를 벗어나면 400.
    """
    try:
        return get_stores_snapshot(_engine, date, time)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get(
    "/api/stores/{store_id}/status/current", tags=["영업유무·혼잡도"], summary="매장 1곳 현재 상태",
    response_model=CurrentStatusOneSchema,
    include_in_schema=False
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
    include_in_schema=False
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
    include_in_schema=False
)
def store_status_day(
    store_id: int = PathParam(..., description="상가 ID (1~21)", examples=[EXAMPLE_STORE_ID]),
    date: date = Query(
        default=EXAMPLE_DATE_REAL,
        description=f"조회할 날짜. {EARLIEST_SAMPLE_DATE}~2026-06-30은 실측, "
                     f"2026-07-01~{LATEST_SERVICE_DATE}는 합성 데이터(is_synthetic로 구분됨).",
    ),
    time: str | None = Query(
        default=None,
        description="기준 시각 'HH:MM'(15분 단위). 이 시각 이후(미래) 슬롯은 응답에서 제외한다. "
                    "생략하면 서버의 현재 시:분을 date에 붙여서 자른다(날짜와 무관하게 항상 "
                    "00:00~그 시:분까지만 나오며, 하루 전체가 새는 일은 없다).",
    ),
):
    """
    예시: `/api/stores/1/status?date=2026-05-15` -> 15분 슬롯별
    schedule_status/power_status/final_status/congestion_level(정수 0~3) + 전력값(kWh).
    화면의 "시간대별 전력" 차트를 이 한 번의 호출로 그릴 수 있다. 기준 시각 이후
    슬롯은 잘라서 보내므로 96개보다 적을 수 있다.
    store_id 범위를 벗어나면 404, date/time이 조회 가능 범위 밖이면 400.
    """
    try:
        result = get_store_status_day(_engine, store_id, date, parse_snapshot_time(time) if time else None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result is None:
        raise HTTPException(status_code=404, detail=f"store_id={store_id}의 매장이 없습니다 (1~21 범위인지 확인하세요)")
    return {
        "store_id": store_id,
        "date": date.isoformat(),
        "data_resolution": result.data_resolution,
        "rows": result.rows,
    }


@app.get(
    "/api/anomalies", tags=["안전감지"], summary="위기 감지 이벤트 목록 (관리자 화면용)",
    response_model=AnomalyListResponse,
)
def list_anomalies(
    level: Literal["주의", "위험"] | None = Query(
        default=None,
        description="안전 등급 필터. 비우면 주의+위험 전부. "
                    "평상시('일반')는 이벤트로 저장되지 않으므로 이 목록에 나오지 않는다.",
        openapi_examples={
            "전체": {"summary": "필터 없음 (주의+위험 전부)", "value": None},
            "위험만": {"summary": "실제 사고 발생 건만", "value": "위험"},
            "주의만": {"summary": "사고 충분조건 건만", "value": "주의"},
        },
    ),
    meter_id: str | None = Query(
        default=None,
        description="특정 계기만 조회. 비우면 21개 매장 전체.",
        openapi_examples={
            "전체": {"summary": "필터 없음 (21개 매장 전체)", "value": None},
            "위험 데모 계기": {"summary": f"{EXAMPLE_DANGER_METER_ID} (7월 위험 시나리오)", "value": EXAMPLE_DANGER_METER_ID},
            "주의 데모 계기": {"summary": f"{EXAMPLE_CAUTION_METER_ID} (7월 주의 시나리오)", "value": EXAMPLE_CAUTION_METER_ID},
        },
    ),
    since: date = Query(
        default=EARLIEST_SAMPLE_DATE,
        description=f"조회 시작일(포함). 데이터 범위: {EARLIEST_SAMPLE_DATE} ~ {LATEST_SERVICE_DATE}.",
    ),
    until: date = Query(
        default=LATEST_SERVICE_DATE,
        description=f"조회 종료일(그날 23:59:59까지 포함). 데이터 범위: {EARLIEST_SAMPLE_DATE} ~ {LATEST_SERVICE_DATE}.",
    ),
    limit: int = Query(default=50, ge=1, le=500, description="한 페이지에 반환할 최대 건수."),
    offset: int = Query(default=0, ge=0, description="건너뛸 건수. 다음 페이지는 offset += limit."),
):
    """
    기본값 그대로 실행하면 전체 기간(2026-04-01\\~2026-07-31)의 최신 50건이 반환된다.

    **안전 등급 3단계**: `일반`(평상시) / `주의`(사고가 나기에 충분한 조건 - 점검 필요) /
    `위험`(실제 사고 발생 - 즉시 조치). `일반`은 "아무 규칙에도 안 걸린 상태"라 이벤트로
    저장되지 않으므로, 이 목록에는 `주의`와 `위험`만 나온다. 특정 슬롯에 이벤트가 없으면
    그 슬롯은 `일반`으로 해석하면 된다.

    **페이징**: 응답의 `total`이 필터 조건에 걸리는 전체 건수다. 다음 페이지는
    `offset`을 `limit`만큼 늘려서 다시 호출한다(예: `?limit=50&offset=50`).
    `offset >= total`이면 빈 배열이 온다.

    **판정 범위**: "영업종료 이후"(closed_hours)에만 판정한다 - 영업시간 중 전력이 높은 건
    혼잡도(`congestion_level`)가 설명할 몫이지 안전 이슈가 아니라고 본다
    (data/프로젝트개요.md 안전감지 항목).

    판정 규칙과 그 전기설비 기준 근거(KEC 212 등)는 `db/docs/안전감지_이상치_판정기준.md` 참고.
    """
    rows, total = get_anomalies(_engine, level, meter_id, since, until, limit, offset)
    return {"total": total, "limit": limit, "offset": offset, "anomalies": rows}


@app.get(
    "/api/meters/{meter_id}/timeseries", tags=["원시 전력값"], summary="계기 1곳의 하루 원시 전력값",
    response_model=MeterTimeseriesResponse,
    include_in_schema=False
)
def meter_timeseries(
    meter_id: str = PathParam(..., description="계기번호(예: 'A-L-11')", examples=[EXAMPLE_METER_ID]),
    date: date = Query(
        default=EXAMPLE_DATE_REAL,
        description=f"조회할 날짜 ({EARLIEST_SAMPLE_DATE} ~ {LATEST_SERVICE_DATE}). "
                    f"2026-06-30까지 실측, 7월은 합성 구간.",
    ),
    time: str | None = Query(
        default=None,
        description="기준 시각 'HH:MM'(15분 단위). 이 시각 이후(미래) 슬롯은 응답에서 제외한다. "
                    "생략하면 서버의 현재 시:분을 date에 붙여서 자른다(날짜와 무관하게 항상 "
                    "00:00~그 시:분까지만 나오며, 하루 전체가 새는 일은 없다).",
    ),
):
    """
    15분 단위 전력값(kWh) + 각 슬롯의 혼잡도(정수 0~3)/영업상태를 반환한다 - 차트를
    이 한 번의 호출로 그릴 수 있게 하기 위함. 기준 시각 이후 슬롯은 잘라서 보낸다.
    """
    try:
        result = get_meter_day_series(_engine, meter_id, date, parse_snapshot_time(time) if time else None)
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
    date: date = Query(
        default=EXAMPLE_DATE_REAL,
        description=f"조회할 날짜 ({EARLIEST_SAMPLE_DATE} ~ {LATEST_SERVICE_DATE}). "
                    f"2026-06-30까지 실측, 7월은 합성 구간.",
    ),
    time: str | None = Query(
        default=None,
        description="기준 시각 'HH:MM'(15분 단위). 이 시각 이후(미래) 슬롯은 응답에서 제외한다. "
                    "생략하면 서버의 현재 시:분을 date에 붙여서 자른다(날짜와 무관하게 항상 "
                    "00:00~그 시:분까지만 나오며, 하루 전체가 새는 일은 없다).",
    ),
):
    """meter_timeseries와 동일하나 store_id(상가 기준)로 조회한다."""
    try:
        result = get_store_day_series(_engine, store_id, date, parse_snapshot_time(time) if time else None)
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
