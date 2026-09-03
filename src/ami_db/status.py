# -*- coding: utf-8 -*-
"""
운영상태(2단계) + 혼잡도 판정 로직.

data/프로젝트개요.md 원문: "영업중 전력량이 영업종료 시간과 같을시 영업 종료로
판단"을 아래 매트릭스로 구체화한다.

  schedule_status(운영시간 vs 현재시각)  x  power_status(현재전력 vs 야간baseline)
  open_hours   + active -> 영업중
  open_hours   + low    -> 휴무추정   (핵심 케이스: 원문의 "영업중인데 전력이 낮음")
  closed_hours + active -> 예외영업   (심야영업 등, 관리자 알림 후보)
  closed_hours + low    -> 영업종료
"""
from __future__ import annotations

from datetime import date, datetime, time

import pandas as pd


def load_effective_hours(conn) -> dict[int, dict[int, dict]]:
    """
    store_id별 · 요일별 "유효" 운영시간을 반환한다. google_places 실측이 있으면
    그걸, 없으면 ksic_estimate를 쓴다 - source 우선순위를 SQL의 DISTINCT ON으로
    한 번에 해결한다(파이썬 쪽에서 별도 fallback 분기를 짜지 않아도 됨).

    반환: {store_id: {day_of_week: {"open_time":.., "close_time":.., "is_closed":.., "is_24h":..}}}
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (store_id, day_of_week)
                   store_id, day_of_week, open_time, close_time, is_closed, is_24h
            FROM store_operating_hours
            ORDER BY store_id, day_of_week, (source = 'google_places') DESC
            """
        )
        rows = cur.fetchall()

    result: dict[int, dict[int, dict]] = {}
    for store_id, dow, open_time, close_time, is_closed, is_24h in rows:
        result.setdefault(store_id, {})[dow] = {
            "open_time": open_time, "close_time": close_time,
            "is_closed": is_closed, "is_24h": is_24h,
        }
    return result


def determine_schedule_status(ts: datetime, day_hours: dict | None) -> str:
    if day_hours is None or day_hours.get("is_closed"):
        return "closed_hours"
    if day_hours.get("is_24h"):
        return "open_hours"
    open_t, close_t = day_hours.get("open_time"), day_hours.get("close_time")
    if open_t is None or close_t is None:
        return "closed_hours"  # 정보 없음은 "영업시간 확인 불가"로, 안전하게 closed 취급
    cur_t = ts.time()
    if close_t < open_t:
        # 자정을 넘기는 영업시간(예: 17:00~익일 04:00, 술집/포장마차류에서 실측으로 확인됨 -
        # "25센치꼬치앤오뎅바" 등). open<=cur<=close 단순 비교로는 이런 경우 항상 False가 되어
        # 매장이 "영업시간 자체가 없는 것"처럼 잘못 판정되므로, close<open이면 자정을 넘긴다고
        # 보고 "open 이후이거나 close 이전"으로 판단한다.
        within = cur_t >= open_t or cur_t <= close_t
    else:
        within = open_t <= cur_t <= close_t
    return "open_hours" if within else "closed_hours"


def determine_power_status(observed_kwh: float | None, night_baseline: float, active_ratio_threshold: float = 1.5) -> str:
    """
    observed가 결측(NaN)이면 "판단 불가"를 low로 방어 처리한다 - 데이터가 없는데
    active로 잘못 판단하는 것보다 안전한 쪽으로 치우치게 하기 위함.
    night_baseline이 0에 가까우면(계기 자체가 매우 저사용) 아주 작은 절대 임계치를
    바닥으로 둬 0 나누기/과민 반응을 막는다.
    """
    if observed_kwh is None or pd.isna(observed_kwh):
        return "low"
    floor = max(night_baseline, 0.01)
    return "active" if observed_kwh >= floor * active_ratio_threshold else "low"


# 스크립트 09가 pandas 벡터화 연산으로 재사용할 수 있도록 매트릭스를 모듈 상수로 노출.
FINAL_STATUS_MATRIX = {
    ("open_hours", "active"): "영업중",
    ("open_hours", "low"): "휴무추정",
    ("closed_hours", "active"): "예외영업",
    ("closed_hours", "low"): "영업종료",
}


def determine_final_status(schedule_status: str, power_status: str) -> str:
    return FINAL_STATUS_MATRIX[(schedule_status, power_status)]


def compute_utilization_quartiles(meter_ts: pd.DataFrame, contract_power_kw: float) -> tuple[float, float]:
    """
    이용률(%) = 15분 kWh를 순간 kW로 환산(x4) / 계약전력 * 100.
    (03_visual_analysis.py의 이용률 계산식과 동일: max_kWh*4/수전전력*100)
    Q1/Q3를 상/중/하 혼잡도 구간 경계로 쓴다(05_시각분석_AI방법론_보고서.md 4-4절:
    소규모·저압 그룹의 이용률 분산이 크다는 실측 근거 - 계기별 상대 기준이 필요).
    """
    if not contract_power_kw or contract_power_kw <= 0:
        return 0.0, 0.0
    util = (meter_ts["recv_kWh"].dropna() * 4) / contract_power_kw * 100
    if util.empty:
        return 0.0, 0.0
    return float(util.quantile(0.25)), float(util.quantile(0.75))


def determine_congestion_level(
    observed_kwh: float | None, contract_power_kw: float, q1: float, q3: float, final_status: str,
) -> str | None:
    """final_status가 '영업중'이 아니면 혼잡도 자체가 의미 없으므로 None(DB엔 NULL)."""
    if final_status != "영업중" or observed_kwh is None or pd.isna(observed_kwh) or not contract_power_kw:
        return None
    util = (observed_kwh * 4) / contract_power_kw * 100
    if util <= q1:
        return "하"
    if util <= q3:
        return "중"
    return "상"
