# -*- coding: utf-8 -*-
"""
실측 프로파일 기반 합성 시계열 생성.

실측 데이터(2026-04-01~06-30)는 시간이 지나면서 "오늘"과의 간격이 벌어지므로,
데모 화면(data/images/user-메인-*.png의 "오늘 시간대별 전력" 뷰)에서 항상 당일
데이터를 볼 수 있으려면 그 이후 구간을 채워야 한다. 절대 무작위 값을 새로
지어내지 않고, 반드시 "그 계기, 그 요일, 그 시간대"의 실측 평균/표준편차에서
샘플링한다 - 이래야 "합성"이 실측 패턴을 벗어나지 않는다.

또한 store_operating_status(2-6단계)와 anomaly_events(2-7단계) 로직이 실제로
뭔가를 검출하는지 보여주려면(실측 3개월에는 자연발생 이상치가 거의 없다는 게
이미 확인돼 있음 - 03_지역특성_규모_업종_분석보고서.md 5-3절, 05_시각분석_AI방법론_보고서.md
5-1절) 합성 구간 중 일부 (계기, 날짜)에 "영업시간 중 야간 수준으로 전력이
떨어지는" 휴업 시나리오와 "야간에 스파이크가 튀는" 이상치 시나리오를 의도적으로
심어야 한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

SLOTS_PER_DAY = 96  # 15분 x 96 = 24시간


def slot_of(t: time) -> int:
    return t.hour * 4 + t.minute // 15


def build_slot_profile(meter_ts: pd.DataFrame) -> pd.DataFrame:
    """
    실측 시계열(한 계기분) -> (요일, 슬롯)별 recv_kWh의 mean/std.
    표준편차를 그대로 노이즈 크기로 쓰는 이유: "무작위 생성 금지, 계기 실측
    패턴 기반이어야 한다"는 요건을 가장 직접적으로 만족시키는 방법이 실측에서
    관측된 변동폭 그대로를 재사용하는 것이기 때문이다.
    """
    df = meter_ts.copy()
    df["dow"] = df["time"].dt.dayofweek  # 0=월요일 ... 6=일요일 (Python 표준과 DB day_of_week 정의를 일치시킴)
    df["slot"] = df["time"].dt.hour * 4 + df["time"].dt.minute // 15
    grouped = (
        df.groupby(["dow", "slot"])["recv_kWh"]
        .agg(mean="mean", std="std")
        .reset_index()
    )
    # 표본 0개인 (요일x슬롯) 그룹은 mean/std가 NaN인 채로 그대로 둔다. 예전에 여기서
    # fillna(0.0)으로 채웠더니 1시간 해상도 계기(A-L-16/19/49/58/70)의 15/30/45분처럼
    # "원래부터 표본이 없는" 슬롯이 합성 구간에서 무조건 정확히 0.0으로 찍히는 버그가
    # 실측 쿼리로 확인됐다 - 매장이 문을 닫은 게 아니라 계산 버그였다. 표본 없음은
    # "모름"이지 "0"이 아니므로 NaN을 그대로 전파해 synthesize_day()가 NULL로 남기게 한다.
    return grouped


def compute_night_baseline(meter_ts: pd.DataFrame) -> float:
    """
    0~5시(0,1,2,3,4시) 구간 recv_kWh의 median.
    평균 대신 median을 쓰는 이유: 순간 스파이크 하나에 기준값이 흔들리지
    않게 하기 위함(이상치 주입 시나리오와 뒤섞이지 않도록).
    """
    night = meter_ts.loc[meter_ts["time"].dt.hour < 5, "recv_kWh"].dropna()
    if len(night) == 0:
        return 0.0
    return float(night.median())


def synthesize_day(profile: pd.DataFrame, target_date: date, rng: np.random.Generator) -> pd.DataFrame:
    """
    profile(build_slot_profile 결과)에서 target_date의 요일에 해당하는 슬롯별
    분포로부터 정규분포 샘플링. 음수 전력은 물리적으로 불가능하므로 0으로 클립.
    """
    dow = target_date.weekday()
    day_profile = profile[profile["dow"] == dow].set_index("slot")

    rows = []
    for slot in range(SLOTS_PER_DAY):
        if slot in day_profile.index:
            mean = float(day_profile.loc[slot, "mean"])
            std = float(day_profile.loc[slot, "std"])
        else:
            mean, std = float("nan"), float("nan")

        if pd.isna(mean):
            # 표본 자체가 없던 슬롯 - 합성도 만들지 않고 NaN(->NULL) 유지.
            # 주의: max(0.0, float('nan'))은 파이썬에서 0.0을 반환한다(NaN이 아님) - 이 함정을
            # 피하려고 mean이 NaN이면 애초에 max()/rng.normal()을 호출하지 않고 여기서 분기한다.
            value = float("nan")
        else:
            value = rng.normal(mean, std) if std > 0 else mean
            value = max(0.0, value)
        hh, mm = divmod(slot * 15, 60)
        rows.append({"slot": slot, "ts": datetime.combine(target_date, time(hh, mm)), "recv_kWh": value})
    return pd.DataFrame(rows)


def inject_closure_scenario(
    day_df: pd.DataFrame, night_baseline: float, closure_start_slot: int, rng: np.random.Generator,
) -> pd.DataFrame:
    """
    closure_start_slot부터 그날 끝까지 전력을 야간 baseline 수준(+미세 노이즈)으로
    덮어쓴다 - "영업시간 중인데 전력이 낮아 휴업으로 판단"되는 사례를 만들기 위함
    (data/프로젝트개요.md의 핵심 판정 로직을 데모에서 실제로 관찰 가능하게 함).
    """
    day_df = day_df.copy()
    mask = day_df["slot"] >= closure_start_slot
    noise_scale = max(night_baseline * 0.05, 0.01)
    noisy = night_baseline + rng.normal(0, noise_scale, size=int(mask.sum()))
    day_df.loc[mask, "recv_kWh"] = np.maximum(0.0, noisy)
    return day_df


def inject_mock_anomaly(
    day_df: pd.DataFrame, spike_slot: int, multiplier: float, rng: np.random.Generator,
) -> pd.DataFrame:
    """
    야간대(0~5시) 특정 슬롯을 급증시켜 이상치 탐지(2-7단계)가 검증할 씨앗 데이터를
    제공한다. 실측 3개월에는 자연발생 이상치가 거의 없어(위 모듈 docstring 근거)
    씨앗 없이는 anomaly_events가 항상 비어있게 되므로 이 주입이 필요하다.
    """
    day_df = day_df.copy()
    current = day_df.loc[day_df["slot"] == spike_slot, "recv_kWh"]
    base_val = float(current.iloc[0]) if len(current) and current.iloc[0] > 0 else 1.0
    day_df.loc[day_df["slot"] == spike_slot, "recv_kWh"] = base_val * multiplier + rng.normal(0, base_val * 0.05)
    return day_df


@dataclass
class ScenarioPick:
    meter_id: str
    target_date: date
    kind: str  # 'closure' | 'anomaly'
    slot: int
    detail: str


def find_open_slot_midpoint(day_hours: dict | None) -> int | None:
    """
    (store_id, day_of_week)의 effective hours(status.load_effective_hours 결과 행 1개,
    dict 형태: open_time/close_time/is_closed)를 받아 "영업시간 한가운데" 슬롯을 고른다.
    휴무일이거나 정보가 없으면 None을 반환해 호출부가 다른 날짜를 시도하게 한다.
    """
    if day_hours is None or day_hours.get("is_closed"):
        return None
    open_t, close_t = day_hours.get("open_time"), day_hours.get("close_time")
    if open_t is None or close_t is None:
        return None
    open_slot, close_slot = slot_of(open_t), slot_of(close_t)
    if close_slot <= open_slot:
        return None
    return open_slot + (close_slot - open_slot) // 2


def select_scenarios(
    meter_ids: list[str],
    synthetic_dates: list[date],
    effective_hours_by_meter: dict[str, dict[int, dict]],
    rng: np.random.Generator,
    n_closure: int = 3,
    n_anomaly: int = 3,
) -> list[ScenarioPick]:
    """
    closure 시나리오는 매칭된 계기 중 첫 번째, anomaly는 두 번째(계기가 1개뿐이면
    같은 계기)에 몰아서 여러 날짜에 심는다 - DoD가 "최소 1건 이상 관찰"을 요구하므로
    여러 날짜에 심어 확률적으로 보수적으로 보장한다.
    """
    picks: list[ScenarioPick] = []
    if not meter_ids or not synthetic_dates:
        return picks

    closure_meter = meter_ids[0]
    anomaly_meter = meter_ids[1] if len(meter_ids) > 1 else meter_ids[0]

    # closure: 실제로 "영업시간 정보가 있는" 날짜를 찾을 때까지 후보를 순회
    hours_by_dow = effective_hours_by_meter.get(closure_meter, {})
    closure_candidates = [
        d for d in synthetic_dates if find_open_slot_midpoint(hours_by_dow.get(d.weekday())) is not None
    ]
    for d in rng.choice(closure_candidates, size=min(n_closure, len(closure_candidates)), replace=False) if closure_candidates else []:
        slot = find_open_slot_midpoint(hours_by_dow.get(d.weekday()))
        picks.append(ScenarioPick(closure_meter, d, "closure", slot, "영업시간 중 야간 baseline 수준으로 전력 급락"))

    anomaly_dates = list(rng.choice(synthetic_dates, size=min(n_anomaly, len(synthetic_dates)), replace=False))
    for d in anomaly_dates:
        # 야간대(0~4시) 중 임의 슬롯 하나
        slot = int(rng.integers(0, 5 * 4))
        picks.append(ScenarioPick(anomaly_meter, d, "anomaly", slot, "야간 시간대 전력 스파이크(mock)"))

    return picks
