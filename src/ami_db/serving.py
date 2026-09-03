# -*- coding: utf-8 -*-
"""
날짜별 15분 시계열 조회.

핵심 설계 포인트: meter_timeseries.is_synthetic이 행마다 이미 저장돼 있으므로,
"이 날짜가 실측 구간이냐 합성 구간이냐"를 판단하는 분기 코드가 이 모듈에는
전혀 없다 - 그냥 날짜로 조회만 하면 각 행이 스스로 실측/합성 여부를 들고 있다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

EARLIEST_SAMPLE_DATE = date(2026, 4, 1)

# 스냅샷 API(get_stores_snapshot) 전용 상한. meter_timeseries.is_synthetic 기준
# 실측 구간은 2026-04-01~06-30까지고 그 이후(07-01~오늘)는 합성 데이터인데,
# 이 엔드포인트는 "AMI 샘플데이터" 자체의 날짜 범위만 받기로 했으므로 다른
# 엔드포인트(EARLIEST_SAMPLE_DATE~date.today())와 달리 상한을 실측 마지막 날짜로
# 고정한다 - date.today()를 쓰지 않는다.
LATEST_SNAPSHOT_DATE = date(2026, 6, 30)

_SNAPSHOT_DATE_RE = re.compile(r"^\d{2}-\d{2}-\d{2}$")
_SNAPSHOT_TIME_RE = re.compile(r"^([01]\d|2[0-3]):(00|15|30|45)$")

# 09_compute_operating_status.py가 실제 계산해 채워두는 4가지 final_status 값 중
# '예외영업'은 프로젝트 내부용 세부 판정값이다(db/docs/영업상태_혼잡도_판정기준.md
# 1-4/1-5절 근거: 07~10시 개점 준비시간에 몰려 발생하는 경우가 절반 이상이라
# "심야 예외영업"이라는 이름과 실제 의미가 어긋나고, 일반 사용자에게 그대로
# 노출하면 오인의 소지가 크다고 해당 문서가 직접 결론 내림). 이 스냅샷 API는
# "일반 사용자" 대상이므로 영업중/영업종료/휴무추정 3값으로 단순화해서 내려준다.
FINAL_STATUS_SIMPLE_MAP = {
    "영업중": "영업중",
    "휴무추정": "휴무추정",
    "예외영업": "영업종료",
    "영업종료": "영업종료",
}

# congestion_level을 프론트가 바로 정렬/비교에 쓸 수 있도록 정수 코드로 내려준다.
# 0은 "혼잡도 판단 불가"를 뜻하며 두 경우를 모두 포함한다: final_status != '영업중'
# (원래도 congestion_level이 NULL) 이거나, 해당 시각의 데이터 자체가 없는 경우.
CONGESTION_LEVEL_CODE = {None: 0, "하": 1, "중": 2, "상": 3}


def _records(df: pd.DataFrame) -> list[dict]:
    """
    DataFrame -> JSON 직렬화 가능한 dict 리스트.

    pandas.read_sql은 컬럼의 모든 값이 NULL이면(예: 21개 매장 전부 지점명이 없는
    branch_name) 그 컬럼을 float64로 추론해 NULL을 np.nan으로 채운다 - 텍스트
    컬럼인데도 그렇다. FastAPI의 기본 JSON 인코더는 NaN을 직렬화하지 못해
    500 에러가 나므로(실측으로 확인함), API 응답으로 내보내기 직전에 항상 이
    함수를 거쳐 NaN을 진짜 None(JSON null)으로 되돌린다.
    """
    return df.replace({np.nan: None}).to_dict(orient="records")


@dataclass
class DaySeriesResult:
    meter_id: str
    target_date: date
    rows: list[dict]  # [{"ts":.., "received_active_power_kwh":.., "is_synthetic":.., "is_redistributed":..}, ...]
    data_resolution: str = "15min"

    @property
    def is_synthetic(self) -> bool | None:
        """그날 데이터가 전부 같은 종류(실측 또는 합성)라는 전제 하에 대표값 하나만 리턴."""
        if not self.rows:
            return None
        return self.rows[0]["is_synthetic"]


def get_meter_day_series(engine: Engine, meter_id: str, target_date: date) -> DaySeriesResult:
    if target_date < EARLIEST_SAMPLE_DATE or target_date > date.today():
        raise ValueError(
            f"target_date는 {EARLIEST_SAMPLE_DATE} ~ {date.today()} 범위여야 합니다 (입력: {target_date})"
        )

    query = text(
        """
        SELECT mt.ts, mt.received_active_power_kwh, mt.is_synthetic, mt.is_redistributed
        FROM meter_timeseries mt
        WHERE mt.meter_id = :meter_id AND mt.ts::date = :target_date
        ORDER BY mt.ts
        """
    )
    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params={"meter_id": meter_id, "target_date": target_date})
        data_resolution = conn.execute(
            text("SELECT data_resolution FROM meters WHERE meter_id = :meter_id"), {"meter_id": meter_id}
        ).scalar_one_or_none() or "15min"

    rows = _records(df)
    return DaySeriesResult(meter_id=meter_id, target_date=target_date, rows=rows, data_resolution=data_resolution)


def get_store_day_series(engine: Engine, store_id: int, target_date: date) -> DaySeriesResult:
    with engine.connect() as conn:
        meter_id = conn.execute(
            text("SELECT meter_id FROM stores WHERE store_id = :store_id"), {"store_id": store_id}
        ).scalar_one_or_none()
    if meter_id is None:
        raise ValueError(f"store_id={store_id}에 해당하는 상가가 없습니다")
    return get_meter_day_series(engine, meter_id, target_date)


# ============================================================
# 아래부터는 화면(메인 목록/지도, 매장 상세, 관리자 안전감지)이 바로 쓸 수 있는
# 조회 함수들. 09/10단계(store_operating_status, anomaly_events)가 이미 배치로
# 계산해 둔 결과를 그대로 읽기만 한다 - 여기서 새로 판정 로직을 계산하지 않는다.
# ============================================================

def get_all_stores(engine: Engine) -> list[dict]:
    """지도/목록 화면용 매장 기본정보 전체(화곡동에 매칭된 상가만, 현재 21개)."""
    query = text(
        """
        SELECT s.store_id, s.meter_id, s.name, s.branch_name, s.road_address, s.building_name, s.floor_info,
               s.longitude, s.latitude, s.biz_category_large, s.biz_category_mid, s.dong_name, s.match_note,
               m.data_resolution
        FROM stores s
        LEFT JOIN meters m ON m.meter_id = s.meter_id
        ORDER BY s.store_id
        """
    )
    with engine.connect() as conn:
        return _records(pd.read_sql(query, conn))


def get_current_status_all(engine: Engine) -> list[dict]:
    """
    모든 매장의 "지금 이 순간" 영업유무/혼잡도.

    store_operating_status는 15분 그리드로 미리 계산돼 있으므로, 매장별로
    "지금 시각 이하인 것 중 가장 최근 행"을 고르면 된다(DISTINCT ON으로 매장당
    1행만 남김). ts <= now()로 제한하는 이유: 이 테이블은 오늘 23:45까지의
    합성 데이터도 이미 갖고 있어서 제한이 없으면 "미래" 슬롯이 최신으로 잡힐 수 있다.
    """
    query = text(
        """
        SELECT DISTINCT ON (sos.store_id)
               sos.store_id, s.name, s.longitude, s.latitude,
               sos.ts, sos.schedule_status, sos.power_status, sos.final_status, sos.congestion_level
        FROM store_operating_status sos
        JOIN stores s ON s.store_id = sos.store_id
        WHERE sos.ts <= now()
        ORDER BY sos.store_id, sos.ts DESC
        """
    )
    with engine.connect() as conn:
        return _records(pd.read_sql(query, conn))


def get_current_status_one(engine: Engine, store_id: int) -> dict | None:
    query = text(
        """
        SELECT sos.store_id, s.name, sos.ts, sos.schedule_status, sos.power_status,
               sos.final_status, sos.congestion_level
        FROM store_operating_status sos
        JOIN stores s ON s.store_id = sos.store_id
        WHERE sos.store_id = :store_id AND sos.ts <= now()
        ORDER BY sos.ts DESC
        LIMIT 1
        """
    )
    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params={"store_id": store_id})
    return _records(df)[0] if not df.empty else None


def parse_snapshot_date(date_str: str) -> date:
    """
    스냅샷 조회용 날짜 파싱. 형식은 'YY-MM-DD'(예: '26-05-09') - ISO 형식(YYYY-MM-DD)과
    다르게 연도를 2자리로 받으라는 요구사항이라 FastAPI 기본 date 타입을 못 쓰고
    문자열로 받아 여기서 직접 파싱/검증한다.
    """
    if not _SNAPSHOT_DATE_RE.match(date_str):
        raise ValueError(f"date는 'YY-MM-DD' 형식이어야 합니다 (입력: {date_str!r}, 예: '26-05-09')")
    try:
        parsed = datetime.strptime(date_str, "%y-%m-%d").date()
    except ValueError as e:
        raise ValueError(f"date를 파싱할 수 없습니다 (입력: {date_str!r}): {e}") from e
    if parsed < EARLIEST_SAMPLE_DATE or parsed > LATEST_SNAPSHOT_DATE:
        raise ValueError(
            f"date는 {EARLIEST_SAMPLE_DATE} ~ {LATEST_SNAPSHOT_DATE} 범위여야 합니다 (입력: {parsed})"
        )
    return parsed


def parse_snapshot_time(time_str: str) -> time:
    """스냅샷 조회용 시각 파싱. 'HH:MM'(00:00~23:45, 15분 단위)만 허용한다."""
    if not _SNAPSHOT_TIME_RE.match(time_str):
        raise ValueError(
            f"time은 'HH:MM' 형식이며 분은 00/15/30/45 중 하나여야 합니다 (입력: {time_str!r}, 예: '19:15')"
        )
    hour_str, minute_str = time_str.split(":")
    return time(int(hour_str), int(minute_str))


def _time_to_str(value) -> str:
    if isinstance(value, str):
        return value[:5]
    return value.strftime("%H:%M")


def _format_store_hours(row: dict | None) -> str:
    if row is None:
        return "정보없음"
    if row.get("is_closed"):
        return "휴무"
    if row.get("is_24h"):
        return "24시간"
    open_t, close_t = row.get("open_time"), row.get("close_time")
    if open_t is None or close_t is None:
        return "정보없음"
    return f"{_time_to_str(open_t)}-{_time_to_str(close_t)}"


def get_stores_snapshot(engine: Engine, date_str: str, time_str: str) -> dict:
    """
    입력된 날짜+시각 한 시점에서 21개 매장 전체의 스냅샷(위치/영업상태/혼잡도/전력사용량)을
    한 번에 반환한다. "이 시각 기준 지도"를 그리려고 매장마다 따로 호출할 필요 없게 만드는
    목적(get_current_status_all의 "지금 이 순간" 버전을 "임의 과거/미래 시각"으로 일반화한 것).

    해상도 처리: 매장의 계기가 data_resolution='1hour'이면 15/30/45분 슬롯 자체가
    없으므로(09_compute_operating_status.py가 결측 슬롯 판정을 생략) 입력 시각의 "시"만
    써서 정각 슬롯을 조회하고, '15min'이면 입력 시각 그대로 조회한다. 매장별로 그 시점
    데이터가 아예 없으면(정각 슬롯 자체가 결측) 상태 관련 필드를 전부 null로 두고
    message에 안내 문구를 채운다 - 없는 데이터를 다른 값으로 대체하지 않는다.

    final_status는 09단계가 실제로 계산하는 4값('영업중'/'휴무추정'/'예외영업'/'영업종료')
    중 '예외영업'을 '영업종료'로 접어 3값으로 단순화해서 내려준다(FINAL_STATUS_SIMPLE_MAP
    주석 참고 - 일반 사용자 대상 API라 "영업중인지 아닌지"만 판단하면 되기 때문).
    """
    target_date = parse_snapshot_date(date_str)
    target_time = parse_snapshot_time(time_str)
    ts_exact = datetime.combine(target_date, target_time)
    ts_hourly = datetime.combine(target_date, time(target_time.hour, 0))

    query = text(
        """
        SELECT
            s.store_id, s.meter_id, s.name, s.road_address, s.longitude, s.latitude,
            s.biz_category_large,
            m.line_name,
            sos.ts AS status_ts, sos.schedule_status, sos.power_status,
            sos.final_status, sos.congestion_level,
            mt.received_active_power_kwh
        FROM stores s
        JOIN meters m ON m.meter_id = s.meter_id
        LEFT JOIN store_operating_status sos
            ON sos.store_id = s.store_id
            AND sos.ts = CASE WHEN m.data_resolution = '1hour' THEN :ts_hourly ELSE :ts_exact END
        LEFT JOIN meter_timeseries mt
            ON mt.meter_id = s.meter_id
            AND mt.ts = CASE WHEN m.data_resolution = '1hour' THEN :ts_hourly ELSE :ts_exact END
        ORDER BY s.store_id
        """
    )
    hours_query = text(
        """
        SELECT DISTINCT ON (store_id) store_id, open_time, close_time, is_closed, is_24h
        FROM store_operating_hours
        WHERE day_of_week = :dow
        ORDER BY store_id, (source = 'google_places') DESC
        """
    )
    with engine.connect() as conn:
        stores_df = pd.read_sql(query, conn, params={"ts_exact": ts_exact, "ts_hourly": ts_hourly})
        hours_df = pd.read_sql(hours_query, conn, params={"dow": target_date.weekday()})

    stores_df["has_data"] = stores_df["status_ts"].notna()
    hours_by_store = {row["store_id"]: row for row in _records(hours_df)}

    groups: dict[str, list[dict]] = {}
    for row in _records(stores_df):
        has_data = bool(row["has_data"])
        hours_row = hours_by_store.get(row["store_id"])
        congestion_level = row["congestion_level"] if has_data else None
        item = {
            "meter_id": row["meter_id"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "congestion_level": CONGESTION_LEVEL_CODE[congestion_level],
            "name": row["name"],
            "road_address": row["road_address"],
            "business_hours": _format_store_hours(hours_row),
            "schedule_status": row["schedule_status"] if has_data else None,
            "power_status": row["power_status"] if has_data else None,
            "final_status": FINAL_STATUS_SIMPLE_MAP.get(row["final_status"]) if has_data else None,
            "received_active_power_kwh": row["received_active_power_kwh"],
            "biz_category_large": row["biz_category_large"],
            "message": None if has_data else "해당 시각에 저장된 데이터가 없습니다",
        }
        groups.setdefault(row["line_name"], []).append(item)

    items = [{"line_name": line_name, "stores": stores} for line_name, stores in groups.items()]
    total_count = sum(len(g["stores"]) for g in items)

    return {"count": total_count, "date": date_str, "time": time_str, "items": items}


# day_of_week(0=월요일...6=일요일, store_operating_hours 컬럼 정의 그대로 - Python
# date.weekday()와 동일한 값)를 응답 키로 쓸 영어 요일명으로 매핑.
_WEEKDAY_KEYS = {
    0: "monday", 1: "tuesday", 2: "wednesday", 3: "thursday",
    4: "friday", 5: "saturday", 6: "sunday",
}


def get_store_detail(engine: Engine, store_id: int) -> dict | None:
    """
    매장 1곳의 상세정보 - POST /api/stores(상세조회, body로 store_id를 받아 URL에
    노출하지 않음)가 쓴다. 평점/전화번호/웹사이트(google_places_cache 최신 1행) +
    요일별(월~일) 영업시간(store_operating_hours, google_places 우선/ksic_estimate
    폴백) + 지금 이 순간의 영업상태/혼잡도(store_operating_status, get_current_status_one과
    동일한 "ts<=now() 중 최신" 조회)를 한 번에 묶어 반환한다.

    google_places_cache는 fetched_at 최신 1행만 쓴다 - scripts/11_refresh_google_places.py가
    실행될 때마다 새 행이 쌓이는 insert-only 테이블이므로(과거 응답 이력 보존이 목적).

    congestion_level은 스냅샷 API(get_stores_snapshot)와 동일하게 CONGESTION_LEVEL_CODE로
    정수 인코딩하고, final_status는 FINAL_STATUS_SIMPLE_MAP으로 3값 단순화한다 - 두
    "일반 사용자" 대상 API의 응답 규칙을 일치시키기 위함.

    반환값 구분: store_id 자체가 stores에 없으면 None(호출부가 404로 매핑) - 다른
    조회 함수들과 동일한 패턴. Google 정보나 현재 상태 데이터가 없는 건 404가 아니라
    해당 필드를 null로 두고 message에 안내 문구를 채워 표현한다(매장 자체는 존재하므로).
    """
    with engine.connect() as conn:
        store_row = conn.execute(
            text("SELECT name, road_address, biz_category_large FROM stores WHERE store_id = :store_id"),
            {"store_id": store_id},
        ).mappings().first()
        if store_row is None:
            return None

        status_row = conn.execute(
            text(
                """
                SELECT schedule_status, power_status, final_status, congestion_level
                FROM store_operating_status
                WHERE store_id = :store_id AND ts <= now()
                ORDER BY ts DESC
                LIMIT 1
                """
            ),
            {"store_id": store_id},
        ).mappings().first()

        places_row = conn.execute(
            text(
                """
                SELECT raw_response_json ->> 'rating' AS rating,
                       raw_response_json ->> 'formatted_phone_number' AS formatted_phone_number,
                       raw_response_json ->> 'website' AS website
                FROM google_places_cache
                WHERE store_id = :store_id
                ORDER BY fetched_at DESC
                LIMIT 1
                """
            ),
            {"store_id": store_id},
        ).mappings().first()

        hours_df = pd.read_sql(
            text(
                """
                SELECT DISTINCT ON (day_of_week) day_of_week, open_time, close_time, is_closed, is_24h
                FROM store_operating_hours
                WHERE store_id = :store_id
                ORDER BY day_of_week, (source = 'google_places') DESC
                """
            ),
            conn,
            params={"store_id": store_id},
        )

    hours_by_dow = {row["day_of_week"]: row for row in _records(hours_df)}
    weekday = {
        _WEEKDAY_KEYS[dow]: _format_store_hours(hours_by_dow.get(dow))
        for dow in range(7)
    }

    messages = []
    if places_row is None:
        messages.append("Google Places 정보가 없어 평점/전화번호/웹사이트를 제공할 수 없습니다")
    if status_row is None:
        messages.append("현재 영업상태 데이터가 없습니다")

    congestion_level = status_row["congestion_level"] if status_row else None
    final_status = FINAL_STATUS_SIMPLE_MAP.get(status_row["final_status"]) if status_row else None
    rating = places_row["rating"] if places_row else None

    return {
        "rating": float(rating) if rating is not None else None,
        "name": store_row["name"],
        "formatted_phone_number": places_row["formatted_phone_number"] if places_row else None,
        "road_address": store_row["road_address"],
        "weekday": weekday,
        "schedule_status": status_row["schedule_status"] if status_row else None,
        "power_status": status_row["power_status"] if status_row else None,
        "final_status": final_status,
        "biz_category_large": store_row["biz_category_large"],
        "website": places_row["website"] if places_row else None,
        "message": "; ".join(messages) if messages else None,
    }


def get_store_hours(engine: Engine, store_id: int) -> list[dict] | None:
    """
    한 매장의 요일별(월~일) "유효" 운영시간 - google_places 실측이 있으면 그걸, 없으면
    ksic_estimate를 쓴다(source 컬럼으로 어느 쪽인지 응답에 그대로 드러냄). 이 우선순위
    SQL은 status.load_effective_hours()의 것과 동일한 아이디어를 이 모듈의 Engine/
    pd.read_sql 관용구로 재현한 것 - 그쪽은 배치 계산용 raw 커넥션이라 커넥션 방식이 다름.

    반환값 구분: store_id 자체가 stores에 없으면 None(호출부가 404로 매핑). 매장은
    있는데 hours 행이 0개면 빈 리스트([]) - 21개 매장은 ksic_estimate가 항상 7행씩
    있어 실무적으로는 발생하지 않지만, 두 상황을 구분해야 404/200(빈 배열)을 정확히
    가를 수 있어 방어적으로 나눠둔다.
    """
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM stores WHERE store_id = :store_id"), {"store_id": store_id}
        ).scalar_one_or_none()
        if exists is None:
            return None

        query = text(
            """
            SELECT DISTINCT ON (day_of_week)
                   day_of_week, open_time, close_time, is_closed, is_24h, source, raw_hours_text
            FROM store_operating_hours
            WHERE store_id = :store_id
            ORDER BY day_of_week, (source = 'google_places') DESC
            """
        )
        df = pd.read_sql(query, conn, params={"store_id": store_id})
    return _records(df)


@dataclass
class DayStatusResult:
    store_id: int
    target_date: date
    rows: list[dict]
    data_resolution: str = "15min"


def get_store_status_day(engine: Engine, store_id: int, target_date: date) -> DayStatusResult | None:
    """
    한 매장의 하루치 상태 타임라인(15분 단위) - 전력값(recv_kWh)까지 같이 조인해서
    반환한다. data/images/user-메인-*.png의 "오늘 시간대별 전력+상태" 뷰를 이 한 번의
    호출로 그릴 수 있게 하려는 목적(전력값 따로, 상태 따로 두 번 호출할 필요 없음).

    data_resolution='1hour'인 매장은 09_compute_operating_status.py가 결측 슬롯의 판정
    자체를 생략(insert 안 함)하므로 rows 길이가 96보다 짧을 수 있다 - 매장당 1개 값으로
    data_resolution을 같이 반환해 호출부(프론트)가 "빈 슬롯=결측 gap"임을 미리 알 수 있게 한다.

    반환값 구분: store_id 자체가 stores에 없으면 None(호출부가 404로 매핑) - get_store_hours()와
    동일한 패턴. 매장은 있는데 그 날짜 행이 0개면 빈 리스트([])가 든 DayStatusResult - 존재하지
    않는 매장과 데이터가 없는 매장을 구분해야 404/200(빈 배열)을 정확히 가를 수 있다.
    """
    if target_date < EARLIEST_SAMPLE_DATE or target_date > date.today():
        raise ValueError(
            f"target_date는 {EARLIEST_SAMPLE_DATE} ~ {date.today()} 범위여야 합니다 (입력: {target_date})"
        )
    query = text(
        """
        SELECT sos.ts, sos.schedule_status, sos.power_status, sos.final_status, sos.congestion_level,
               mt.received_active_power_kwh, mt.is_synthetic, mt.is_redistributed
        FROM store_operating_status sos
        JOIN stores s ON s.store_id = sos.store_id
        JOIN meter_timeseries mt ON mt.meter_id = s.meter_id AND mt.ts = sos.ts
        WHERE sos.store_id = :store_id AND sos.ts::date = :target_date
        ORDER BY sos.ts
        """
    )
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM stores WHERE store_id = :store_id"), {"store_id": store_id}
        ).scalar_one_or_none()
        if exists is None:
            return None

        df = pd.read_sql(query, conn, params={"store_id": store_id, "target_date": target_date})
        data_resolution = conn.execute(
            text(
                "SELECT m.data_resolution FROM stores s JOIN meters m ON m.meter_id = s.meter_id "
                "WHERE s.store_id = :store_id"
            ),
            {"store_id": store_id},
        ).scalar_one_or_none() or "15min"
    return DayStatusResult(store_id=store_id, target_date=target_date, rows=_records(df), data_resolution=data_resolution)


def get_anomalies(
    engine: Engine,
    level: str | None = None,
    meter_id: str | None = None,
    since: date | None = None,
    until: date | None = None,
    limit: int = 200,
) -> list[dict]:
    """
    안전감지(관리자 화면)용 이상치 이벤트 목록. 상가 이름까지 조인해서 반환한다
    (관리자가 계기번호보다 매장명으로 보는 게 자연스러움). 최신순 정렬.
    """
    conditions: list[str] = []
    params: dict = {"limit": limit}
    if level is not None:
        conditions.append("ae.level = :level")
        params["level"] = level
    if meter_id is not None:
        conditions.append("ae.meter_id = :meter_id")
        params["meter_id"] = meter_id
    if since is not None:
        conditions.append("ae.detected_at >= :since")
        params["since"] = since
    if until is not None:
        conditions.append("ae.detected_at <= :until")
        params["until"] = until
    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    query = text(
        f"""
        SELECT ae.event_id, ae.meter_id, s.name AS store_name, ae.detected_at, ae.level,
               ae.rule_triggered, ae.metric_value, ae.threshold_value, ae.notified_at
        FROM anomaly_events ae
        LEFT JOIN stores s ON s.meter_id = ae.meter_id
        {where_clause}
        ORDER BY ae.detected_at DESC
        LIMIT :limit
        """
    )
    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params=params)
    return _records(df)
