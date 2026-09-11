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

import numpy as np
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


# db/problem/문제상황-1.txt: "여유/보통/혼잡 기준이 명확하지 않거나 계산법이 잘못된 것
# 같다 - 각 매장의 운영시간을 분포도로 나타낸다면 어떨까? 동시간 최대 1달을 기준으로
# 해보자(계절성 고려하면 3개월이 이상적이나 실측이 90일뿐이라 1달로 타협)"에 따라
# 계약전력 대비 %로 하루 전체를 통짜로 비교하던 예전 방식(compute_utilization_quartiles/
# determine_congestion_level, 둘 다 삭제됨)을 대체한다. 계약전력 기준은 "과부하 위험에
# 얼마나 가까운가"를 보는 안전감지(anomaly.py)의 척도이지, 사용자 화면의 "혼잡도"가
# 보여주려는 "평소보다 붐비는가"와는 목적이 다르다고 판단해, 매장 자신의 동시간대
# 원본 kWh 분포로 바꿨다.
CONGESTION_WINDOW_MINUTES = 30  # 동시간대 판정에서 앞뒤로 묶는 폭(±30분 = 15분 슬롯 5개)
CONGESTION_LOOKBACK_DAYS = 30   # 기준 산정에 쓰는 최근 실측 일수
CONGESTION_MIN_SAMPLES = 5      # 이 미만이면 그 시간대만의 기준을 못 믿고 매장 전체로 폴백


def compute_congestion_thresholds(
    real_open: pd.DataFrame,
    window_minutes: int = CONGESTION_WINDOW_MINUTES,
    lookback_days: int = CONGESTION_LOOKBACK_DAYS,
    min_samples: int = CONGESTION_MIN_SAMPLES,
) -> tuple[dict[int, tuple[float, float]], tuple[float, float] | None]:
    """
    매장 "자기 자신의 동시간대 분포"로 혼잡도 경계(Q1/Q3)를 잡는다.

    real_open: 그 매장의 "실측(is_synthetic=False) + 영업중으로 판정된" 슬롯만
        (ts, recv_kWh 두 컬럼). 합성 구간(7월, 안전감지 데모용으로 위험/주의 시나리오가
        일부러 심긴 이상치 구간)을 기준 산정에 섞으면 기준 자체가 오염되므로 호출부가
        미리 걸러서 넘겨야 한다.

    - 최근 lookback_days일(그 매장 실측 범위 안에서)만 쓴다 - 계절성을 고려하면 3개월이
      이상적이나 주어진 실측이 91일뿐이라 1개월로 타협한 것(db/problem/문제상황-1.txt).
    - "동시간대"를 글자 그대로 같은 시:분만 모으면(예: 매일 14:00 하나) 30일치라
      슬롯당 표본이 ~30개뿐이라 분위수가 노이즈에 민감해진다. ±window_minutes(기본
      30분 = 앞뒤 슬롯 2개씩 총 5개)를 자정을 넘나드는 원형 거리로 묶어 표본을
      ~5배로 늘린다(요일 구분은 하지 않음 - 요일까지 쪼개면 표본이 30/7 ≈ 4개로
      줄어 오히려 못 믿을 값이 된다).
    - 표본이 min_samples 미만인 시간대는 그 시간대만의 기준을 신뢰할 수 없으므로,
      이 매장의 "동시간대 구분 없는 전체" Q1/Q3(fallback)로 대체한다 - 특정 시간대만
      운영 시작 초기라 표본이 적은 경우 등 드문 예외를 위한 안전장치다.

    반환: ({슬롯(자정 이후 분): (q1,q3)}, fallback(q1,q3) 또는 표본 자체가 없으면 None).
    """
    if real_open.empty:
        return {}, None

    cutoff = real_open["ts"].max() - pd.Timedelta(days=lookback_days - 1)
    recent = real_open.loc[real_open["ts"] >= cutoff]
    if recent.empty:
        recent = real_open

    vals_all = recent["recv_kWh"].dropna().to_numpy()
    fallback = (
        (float(np.quantile(vals_all, 0.25)), float(np.quantile(vals_all, 0.75)))
        if vals_all.size else None
    )

    tod = (recent["ts"].dt.hour * 60 + recent["ts"].dt.minute).to_numpy()
    vals = recent["recv_kWh"].to_numpy()
    day_minutes = 24 * 60

    thresholds: dict[int, tuple[float, float]] = {}
    for slot in np.unique(tod):
        diff = np.abs(tod - slot)
        circ = np.minimum(diff, day_minutes - diff)  # 자정 경계를 넘나드는 원형 거리
        pooled = vals[circ <= window_minutes]
        pooled = pooled[~np.isnan(pooled)]
        if pooled.size < min_samples:
            continue
        thresholds[int(slot)] = (float(np.quantile(pooled, 0.25)), float(np.quantile(pooled, 0.75)))

    return thresholds, fallback
