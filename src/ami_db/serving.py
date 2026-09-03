# -*- coding: utf-8 -*-
"""
날짜별 15분 시계열 조회.

핵심 설계 포인트: meter_timeseries.is_synthetic이 행마다 이미 저장돼 있으므로,
"이 날짜가 실측 구간이냐 합성 구간이냐"를 판단하는 분기 코드가 이 모듈에는
전혀 없다 - 그냥 날짜로 조회만 하면 각 행이 스스로 실측/합성 여부를 들고 있다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

EARLIEST_SAMPLE_DATE = date(2026, 4, 1)


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
