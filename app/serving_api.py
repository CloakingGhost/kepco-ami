# -*- coding: utf-8 -*-
"""
data/프로젝트개요.md 개발우선순위 1번(영업유무·혼잡도)과 2번(안전감지 이상치 조회)에
대응하는 조회용 FastAPI. 포트 8000(Google Places 호출은 04단계 배치 스크립트 안에서
인프로세스로만 발생 - places-api-project는 더 이상 서버로 뜨지 않아 포트 충돌이 없음).

- 1번(영업유무/혼잡도): /api/stores*가 담당 - store_operating_status를 09_compute_operating_status.py가
  미리 계산해 둔 결과를 그대로 읽는다. store_id/meter_id처럼 특정 대상을 지목하는 조회는 URL에
  ID를 노출하지 않도록 전부 POST + body로 받는다(예: POST /api/stores/status/day).
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

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query  # noqa: E402

from ami_db.config import settings  # noqa: E402
from ami_db.db import get_engine  # noqa: E402
from ami_db.serving import (  # noqa: E402
    EARLIEST_SAMPLE_DATE,
    LATEST_SERVICE_DATE,
    LATEST_SNAPSHOT_DATE,
    claim_narration,
    get_all_stores,
    get_anomalies,
    get_anomaly_event,
    get_anomaly_period,
    get_anomaly_snapshot,
    get_current_status_all,
    get_current_status_one,
    get_meter_day_series,
    get_narration,
    get_store_day_series,
    get_store_detail,
    get_store_hours,
    get_store_status_day,
    get_stores_snapshot,
    parse_snapshot_time,
    save_narration_done,
    save_narration_failed,
)
from ami_db.narrate import narrate_event  # noqa: E402
from app.schemas import (  # noqa: E402
    AnomalyExplainRequest,
    AnomalyExplainResponse,
    AnomalyListResponse,
    AnomalyPeriodResponse,
    AnomalySnapshotResponse,
    CurrentStatusListResponse,
    CurrentStatusOneSchema,
    DayStatusResponse,
    MeterTimeseriesRequest,
    MeterTimeseriesResponse,
    StoreDetailRequest,
    StoreDetailResponse,
    StoreHoursResponse,
    StoreIdRequest,
    StoreListResponse,
    StoreSnapshotResponse,
    StoreStatusDayRequest,
    StoreTimeseriesRequest,
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
# 자세한 매핑은 db/docs/API_REFERENCE.md 참고. store_id/meter_id 자체의 예시값은 이제
# body 스키마 쪽(app/schemas.py)에 있으므로 여기서는 anomalies 필터에서만 쓰는 값만 남긴다.
EXAMPLE_DANGER_METER_ID = "A-L-60"          # 7월 '위험' 시나리오가 심긴 계기(충북식당)
EXAMPLE_CAUTION_METER_ID = "A-L-65"         # 7월 '주의' 시나리오가 심긴 계기(와카츠)
EXAMPLE_REPEAT_METER_ID = "A-L-63"          # 7월 '주의반복'(하루 3회) 시나리오가 심긴 계기(소문난순대)


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
                    f"(2026-06-30까지 실측, 7월은 안전감지 데모용 합성 구간).",
    ),
    time: str = Query(
        default="19:15",
        description="조회할 시각, 'HH:MM' 형식(00:00~23:45, 15분 단위만 허용: 00/15/30/45).",
    ),
):
    """
    예시: `/api/stores/snapshot?date=26-05-09&time=19:15` -> 그 시점 21개 매장의
    위치/영업상태/혼잡도/전력사용량을 한 번에 반환한다.

    조회 가능 날짜 범위는 2026-04-01~2026-07-31이다. 06-30까지가 실측이고 7월은
    안전감지 데모용 합성 구간이라, 7월을 조회하면 화면에서 합성 데이터임을 안내해야 한다
    (원래는 실측 구간만 받았는데, 안전감지 데모가 7월에만 있어 지도와 안전감지 패널이
    서로 다른 날짜를 봐야 하는 문제가 있어 넓혔다).

    계기 해상도: 21개 매장 전부 15분 데이터를 가진다 - 원천 계기가 1시간 적산값만 보고하는
    5개 매장도 적재 단계에서 15분 구간 4개로 분해했다(해당 값은 시계열 API의
    is_redistributed=true로 구분). 매장별로 그 시점 데이터 자체가 없으면 상태 관련 필드가
    전부 null이 되고 message에 안내 문구가 채워진다.

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


@app.post(
    "/api/stores/status/current", tags=["영업유무·혼잡도"], summary="매장 1곳 현재 상태",
    response_model=CurrentStatusOneSchema,
)
def store_current_status(body: StoreIdRequest):
    """
    store_id를 body로 받는다(URL에 store_id를 노출하지 않으려고 POST를 씀).
    예시: body `{"store_id": 1}` -> 못난이찹쌀꽈배기의 현재 상태.
    store_id 범위를 벗어나거나(1~21이 아니거나) 아직 상태가 계산되지 않았으면 404.
    """
    result = get_current_status_one(_engine, body.store_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"store_id={body.store_id}의 상태 데이터가 없습니다 (1~21 범위인지 확인하세요)")
    return result


@app.post(
    "/api/stores/hours", tags=["영업유무·혼잡도"], summary="매장 1곳의 요일별 운영시간",
    response_model=StoreHoursResponse,
)
def store_hours(body: StoreIdRequest):
    """
    store_id를 body로 받는다(URL에 store_id를 노출하지 않으려고 POST를 씀).
    예시: body `{"store_id": 1}` -> 못난이찹쌀꽈배기의 요일별(월~일) 운영시간.
    google_places 실측이 있으면 그걸, 없으면 ksic_estimate(업종코드 기반 추정)를
    반환한다 - 각 행의 `source`로 어느 쪽인지 구분된다. store_id 범위를 벗어나면 404.
    """
    rows = get_store_hours(_engine, body.store_id)
    if rows is None:
        raise HTTPException(status_code=404, detail=f"store_id={body.store_id}의 매장이 없습니다 (1~21 범위인지 확인하세요)")
    return {"store_id": body.store_id, "hours": rows}


@app.post(
    "/api/stores/status/day", tags=["영업유무·혼잡도"], summary="매장 1곳의 하루 상태+전력 타임라인",
    response_model=DayStatusResponse,
)
def store_status_day(body: StoreStatusDayRequest):
    """
    store_id를 body로 받는다(URL에 store_id를 노출하지 않으려고 POST를 씀).

    예시: body `{"store_id": 1, "date": "2026-05-15"}` -> 15분 슬롯별
    schedule_status/power_status/final_status/congestion_level(정수 0~3) + 전력값(kWh).
    화면의 "시간대별 전력" 차트를 이 한 번의 호출로 그릴 수 있다.

    **기준 시각**: body에 time을 같이 주면 그 시각 이후(미래) 슬롯은 잘라서 응답에서
    제외한다(96개보다 적을 수 있음) - 화면에서 사용자가 고른 기준 시각을 그대로 넘기면
    차트가 그 시각 이후로 새지 않는다. 생략하면 서버의 현재 시:분 기준.

    store_id 범위를 벗어나면 404, date/time이 조회 가능 범위 밖이면 400.
    """
    try:
        result = get_store_status_day(
            _engine, body.store_id, body.date, parse_snapshot_time(body.time) if body.time else None
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result is None:
        raise HTTPException(status_code=404, detail=f"store_id={body.store_id}의 매장이 없습니다 (1~21 범위인지 확인하세요)")
    return {
        "store_id": body.store_id,
        "date": body.date.isoformat(),
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
    "/api/anomalies/snapshot", tags=["안전감지"], summary="특정 시점 위기 감지 스냅샷 (화면 표시용)",
    response_model=AnomalySnapshotResponse,
)
def anomalies_snapshot(
    date: str | None = Query(
        default=None,
        description=f"조회할 날짜, 'YY-MM-DD' 형식(연도 2자리). 범위: "
                    f"{EARLIEST_SAMPLE_DATE.strftime('%y-%m-%d')} ~ {LATEST_SERVICE_DATE.strftime('%y-%m-%d')}. "
                    "생략하면 time과 무관하게 서버의 현재 날짜를 쓴다.",
        openapi_examples={
            "현재": {"summary": "생략 (서버 현재 시각 기준)", "value": None},
            "위험 데모": {"summary": f"{EXAMPLE_DANGER_METER_ID} 위험 시나리오 당일", "value": "26-07-02"},
            "주의반복 데모": {"summary": f"{EXAMPLE_REPEAT_METER_ID} 주의반복 시나리오 당일", "value": "26-07-22"},
        },
    ),
    time: str | None = Query(
        default=None,
        description="조회할 시각, 'HH:MM' 형식(00:00~23:45, 15분 단위만 허용: 00/15/30/45). "
                    "생략하면 date와 무관하게 서버의 현재 시:분(15분 단위로 내림)을 쓴다.",
        openapi_examples={
            "현재": {"summary": "생략 (서버 현재 시각 기준)", "value": None},
            "위험 데모": {"summary": "위험 시나리오 지속 구간 한가운데", "value": "02:15"},
            "주의반복 데모": {"summary": "3번째 반복 사건 종료 직후", "value": "06:00"},
        },
    ),
):
    """
    화면에 즉시 띄울 매장만 골라 돌려주는 스냅샷 API - 관리자용 전체 이력 조회(`GET /api/anomalies`)와
    달리, 아래 두 상황 중 하나에 걸린 매장만 `alerts`에 담는다. 둘 다 아니면(대부분의 매장·시각)
    응답은 `count=0`, `alerts=[]`다.

    1. **즉시위험**: 조회 시점의 슬롯에 `위험` 이벤트가 있는 매장(`kec212_overload_130pct_60min`).
    2. **주의반복**: 조회 시점 기준 최근 24시간 안에 서로 다른 `주의` 사건(슬롯 간격이 15분을
       넘으면 별개 사건으로 취급)이 3번 이상 있는 매장. 사건 하나가 이미 여러 슬롯(행)에 걸치므로
       원시 행 개수가 아니라 사건 개수로 센다 - 자세한 근거는 `db/docs/안전감지_이상치_판정기준.md` 참고.

    date/time을 둘 다 생략하면 서버 "현재" 기준(`ami_db.serving.service_now`)으로 조회한다 -
    단, 실측 데이터는 2026-06-30에서 끝나고 위험/주의 데모는 7월 합성 구간에만 있으므로, 데모
    확인 목적이라면 위 예시값(위험: 26-07-02 02:15, 주의반복: 26-07-22 06:00)으로 직접 지정해서
    호출해야 한다.
    """
    try:
        return get_anomaly_snapshot(_engine, date, time)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get(
    "/api/anomalies/period", tags=["안전감지"], summary="그 달 1일부터 지정 시점까지 누적 조회",
    response_model=AnomalyPeriodResponse,
)
def anomalies_period(
    date: str | None = Query(
        default=None,
        description=f"조회 기준 날짜 'YY-MM-DD'. 범위: "
                    f"{EARLIEST_SAMPLE_DATE.strftime('%y-%m-%d')} ~ "
                    f"{LATEST_SERVICE_DATE.strftime('%y-%m-%d')}. 생략하면 서버 현재 날짜.",
        openapi_examples={
            "5월 중순": {"summary": "실측 구간 - 05-01 00:00~05-13 16:00", "value": "26-05-13"},
            "7월 데모": {"summary": "합성 구간 - 07-01 00:00~07-22 06:00", "value": "26-07-22"},
            "현재": {"summary": "생략(서버 현재 기준)", "value": None},
        },
    ),
    time: str | None = Query(
        default=None,
        description="조회 기준 시각 'HH:MM'(15분 단위). 생략하면 서버 현재 시:분.",
        openapi_examples={
            "오후 4시": {"summary": "16:00", "value": "16:00"},
            "현재": {"summary": "생략", "value": None},
        },
    ),
):
    """
    **그 달 1일 00:00부터 요청한 시점까지**의 안전감지 이력을 매장별로 묶어서 돌려준다.

    스냅샷(`/api/anomalies/snapshot`)은 "지금 이 15분 슬롯"만 보기 때문에 사건이 없는
    시각을 고르면 늘 비어 있다. 이 엔드포인트는 누적 구간을 보므로 "이번 달에 어느
    매장에 무슨 일이 있었는지"를 한 번에 확인할 수 있다.

    예: `?date=26-05-13&time=16:00` -> **2026-05-01 00:00 ~ 2026-05-13 16:00** 구간

    **사건(episode) 단위로 묶어서 준다.** `anomaly_events`는 15분 슬롯당 1행이라 60분짜리
    사건 하나가 4행으로 남는데, 그대로 내려보내면 화면에서 같은 사건이 네 번 반복되는
    것처럼 보인다. 슬롯 간격이 15분을 넘거나 등급·규칙이 바뀌면 다른 사건으로 끊는다.

    매장 정렬은 **위험이 있는 매장 먼저, 그다음 건수 많은 순**이라 앞에서부터 그리면 된다.
    """
    try:
        return get_anomaly_period(_engine, date, time)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# AI 분석 응답의 안내문. **실패를 HTTP 에러로 던지지 않는다.** 분석이 아직 없거나 외부
# 모델이 죽은 것은 "시스템이 고장 난" 게 아니라 "분석이 아직/지금은 없는" 상태이고,
# 사용자는 에러 화면이 아니라 무슨 상황인지를 봐야 한다. 감지(규칙 판정) 자체는 AI와
# 무관하게 이미 정확하므로 그 사실을 함께 알린다.
_ANALYSIS_MESSAGES = {
    "none": "아직 AI 분석을 요청하지 않았습니다.",
    "pending": "AI가 분석하고 있습니다. 완료되면 이 자리에 표시됩니다.",
    "failed": "AI 분석을 완료하지 못했습니다. 잠시 후 다시 요청해 주세요. 감지 결과는 그대로 정확합니다.",
    "unavailable": "지금은 AI 분석을 사용할 수 없습니다(시스템 설정 문제). 감지 결과는 그대로 정확합니다.",
}


def _find_event_or_404(body: AnomalyExplainRequest) -> dict:
    event = get_anomaly_event(_engine, body.store_id, body.detected_at)
    if event is None:
        raise HTTPException(
            status_code=404,
            detail=f"store_id={body.store_id}, detected_at={body.detected_at.isoformat()}에 "
                   "해당하는 감지 이벤트가 없습니다 (GET /api/anomalies/snapshot의 값을 그대로 넘기세요).",
        )
    return event


def _analysis_response(event: dict, row: dict | None, *, override: str | None = None) -> dict:
    """규칙 판정 결과(event) + 저장된 분석(anomaly_narrations 행) -> AnomalyExplainResponse."""
    base = {
        "store_id": event["store_id"],
        "store_name": event["store_name"],
        "detected_at": event["detected_at"],
        "level": event["level"],
        "rule_triggered": event["rule_triggered"],
    }
    if override:  # 'unavailable' - DB에 아무것도 쓰지 않고 안내만 돌려준다
        return {**base, "status": "failed", "message": _ANALYSIS_MESSAGES[override]}
    if row is None:
        return {**base, "status": "none", "message": _ANALYSIS_MESSAGES["none"]}

    status = row["status"]
    # 서버 재기동으로 작업이 사라진 오래된 pending은 실패로 보여줘야 다시 요청할 수 있다
    # (claim_narration도 같은 기준으로 재시작을 허용한다).
    if status == "pending" and row["stale"]:
        status = "failed"
    if status != "done":
        return {**base, "status": status, "message": _ANALYSIS_MESSAGES[status],
                "requested_at": row["requested_at"]}
    return {
        **base,
        "status": "done",
        "message": None,
        "owner_sms": row["owner_sms"],
        "admin_note": row["admin_note"],
        "emergency_report": row["emergency_report"],
        "next_steps": row["next_steps"] or [],
        "inquiry_draft": row["inquiry_draft"],
        "verification_passed": row["verification_passed"],
        "unknown_numbers": row["unknown_numbers"] or [],
        "model": row["model"],
        "elapsed_ms": row["elapsed_ms"],
        "requested_at": row["requested_at"],
        "completed_at": row["completed_at"],
    }


def _run_analysis_job(event: dict) -> None:
    """
    백그라운드 작업: AI 생성 -> 성공하면 DB에 done, 실패하면 failed로 남긴다.

    요청 핸들러는 이 작업을 걸어두고 즉시 'pending'으로 응답한다. 살아 있는 모델이 전부
    24~57초(2026-09-10 실측)라 요청을 붙잡고 기다리면 앞단 프록시(30초)가 먼저 연결을
    끊어 사용자는 결국 에러를 보게 된다 - 생성과 응답을 분리한 이유다.
    """
    try:
        result = narrate_event(event)
    except Exception as e:  # noqa: BLE001 - 어떤 실패든 요청자에겐 'failed' 상태로 전달된다
        detail = f"{type(e).__name__}: {e}"[:500]
        print(f"[ai-analysis] event_id={event['event_id']} 실패 - {detail}", flush=True)
        save_narration_failed(_engine, event["event_id"], detail)
        return
    save_narration_done(_engine, event["event_id"], result)
    print(f"[ai-analysis] event_id={event['event_id']} 완료 "
          f"({result.model}, {result.elapsed_ms}ms)", flush=True)


@app.post(
    "/api/anomalies/explain", tags=["안전감지"],
    summary="AI 분석 요청 (저장본이 있으면 즉시 반환, 없으면 생성 시작)",
    response_model=AnomalyExplainResponse,
)
def request_anomaly_analysis(body: AnomalyExplainRequest, background_tasks: BackgroundTasks):
    """
    감지 이벤트에 대한 AI 분석(점주 문자 / 점검 사유 / 신고 초안 / 대처방안 / 문의 초안)을
    **요청한다.** 분석은 사용자가 요청했을 때만 만들고, 만든 결과는 DB(`anomaly_narrations`)에
    저장해 다음부터는 그대로 읽는다. 감지된 이벤트마다 미리 만들어 두지 않는다.

    - 이미 분석된 이벤트 -> `status='done'`과 저장된 결과를 **즉시** 반환(AI를 다시 부르지 않음)
    - 처음 요청 -> 백그라운드에서 생성을 시작하고 즉시 `status='pending'`으로 응답.
      완료 여부는 `POST /api/anomalies/explain/status`로 확인한다.
    - 이전 시도가 실패했으면(`failed`) 다시 요청할 때 새로 시도한다.

    **AI 실패는 HTTP 에러가 아니다.** 외부 모델이 죽었거나 키가 없어도 200과 함께
    `status='failed'`와 화면에 그대로 띄울 안내문(`message`)을 준다. 404는 해당
    (store_id, detected_at) 감지 이벤트 자체가 없을 때뿐이다.

    **LLM은 판정에 관여하지 않는다.** 등급(`level`)과 발동 규칙(`rule_triggered`)은 규칙이
    이미 확정한 값이고(규칙 F1 0.698 vs Isolation Forest 0.582, KEC 212.3 법정 근거),
    수치는 클라이언트 입력이 아니라 `anomaly_events`에서 다시 읽는다. 생성된 문장의 숫자는
    입력 수치와 자동 대조해 **검증을 통과한 결과만 저장한다.**
    """
    event = _find_event_or_404(body)
    row = get_narration(_engine, event["event_id"])
    if row and row["status"] == "done":
        return _analysis_response(event, row)  # DB 저장본 - AI를 다시 부르지 않는다
    if not settings.nvidia_api_key:
        return _analysis_response(event, None, override="unavailable")
    if claim_narration(_engine, event["event_id"]):
        background_tasks.add_task(_run_analysis_job, event)
    # 다시 읽는다 - 방금 이 요청이 맡은 pending일 수도, 다른 요청이 이미 진행 중이거나
    # 끝낸 상태일 수도 있다(같은 이벤트를 두 사람이 동시에 눌러도 작업은 하나만 돈다).
    return _analysis_response(event, get_narration(_engine, event["event_id"]))


@app.post(
    "/api/anomalies/explain/status", tags=["안전감지"],
    summary="AI 분석 상태 조회 (새로 생성하지 않음)",
    response_model=AnomalyExplainResponse,
)
def get_anomaly_analysis_status(body: AnomalyExplainRequest):
    """
    AI 분석의 현재 상태를 **읽기만** 한다 - 여기서는 절대 생성을 시작하지 않는다.
    `POST /api/anomalies/explain`이 `pending`을 돌려줬을 때 완료를 확인하는 용도다.

    `status`: `none`(요청된 적 없음) | `pending`(생성 중) | `done`(완료, 결과 포함) |
    `failed`(실패 - 다시 요청 가능). 생성 중 서버가 재기동돼 작업이 사라진 경우도
    일정 시간(`NARRATION_STALE_MINUTES`)이 지나면 `failed`로 보여 다시 요청할 수 있다.
    """
    event = _find_event_or_404(body)
    return _analysis_response(event, get_narration(_engine, event["event_id"]))


@app.post(
    "/api/meters/timeseries", tags=["원시 전력값"], summary="계기 1곳의 하루 원시 전력값",
    response_model=MeterTimeseriesResponse,
)
def meter_timeseries(body: MeterTimeseriesRequest):
    """
    meter_id를 body로 받는다(URL에 meter_id를 노출하지 않으려고 POST를 씀).

    15분 단위 전력값(kWh) + 각 슬롯의 혼잡도(정수 0~3)/영업상태를 반환한다 - 차트를
    이 한 번의 호출로 그릴 수 있게 하기 위함. body에 time을 주면 그 시각 이후(미래)
    슬롯은 잘라서 응답에서 제외한다.
    """
    try:
        result = get_meter_day_series(
            _engine, body.meter_id, body.date, parse_snapshot_time(body.time) if body.time else None
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "meter_id": result.meter_id,
        "date": result.target_date.isoformat(),
        "is_synthetic": result.is_synthetic,
        "data_resolution": result.data_resolution,
        "rows": result.rows,
    }


@app.post(
    "/api/stores/timeseries", tags=["원시 전력값"], summary="매장 1곳의 하루 원시 전력값",
    response_model=StoreTimeseriesResponse,
)
def store_timeseries(body: StoreTimeseriesRequest):
    """
    store_id를 body로 받는다(URL에 store_id를 노출하지 않으려고 POST를 씀).
    meter_timeseries와 동일하나 store_id(상가 기준)로 조회한다.
    """
    try:
        result = get_store_day_series(
            _engine, body.store_id, body.date, parse_snapshot_time(body.time) if body.time else None
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "store_id": body.store_id,
        "meter_id": result.meter_id,
        "date": result.target_date.isoformat(),
        "is_synthetic": result.is_synthetic,
        "data_resolution": result.data_resolution,
        "rows": result.rows,
    }
