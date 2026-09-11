# -*- coding: utf-8 -*-
"""
1시간 적산 계기(recv_kWh가 매시 정각에만 있는 계기) 탐지 + 15분 구간 분해.

화곡동 매칭 21개 중 5개 계기(A-L-16/19/49/58/70)는 에너지 레지스터(kWh·kVAh·무효전력량)가
매시 정각에만 찍힌다. 그 정각값은 "15분값 하나만 남고 셋이 유실된 것"이 아니라 **정각 T로
끝나는 1시간(T-45, T-30, T-15, T로 끝나는 15분 구간 4개)의 적산값**이다 - 같은 계기의 15분
평균 전압·전류로 만든 피상전력을 그 4개 구간에 걸쳐 적분하면 정각 kVAh와 ±0.3% 이내로
일치한다. 근거와 검증 수치는 db/docs/시간적산계기_15분분해_분석보고서.md.

그래서 정각 적산값을 그 4개 구간에 V×I 비율로 나눠 담는다(split_hourly_energy). 시간 합계가
실측 그대로 보존되고 역률 가정이 필요 없다. 전압·전류가 아예 없는 계기(A-L-49)는 모양
정보가 없으니 4등분한다. 분해된 슬롯은 전부 is_redistributed=True.

원천 pkl을 읽는 모든 단계(02 적재, 06 합성, 09 상태, 10 안전감지)가 load_real_timeseries()로
같은 분해 결과를 봐야 한다 - 한 곳이라도 원본 pkl을 직접 읽으면 그 단계만 1시간 적산
스케일(15분값의 ~4배)로 계산하게 된다.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import numpy as np
import pandas as pd

RECV_ENERGY_COLS = ["recv_kVAh", "recv_kWh", "recv_lag_kvarh", "recv_lead_kvarh"]
_HOUR_WINDOW = [pd.Timedelta(minutes=m) for m in (45, 30, 15, 0)]  # 정각 T로 끝나는 15분 구간 4개


def detect_data_resolution(meter_ts: pd.DataFrame, min_samples: int = 100) -> str:
    """
    meter_ts: 'time'/'recv_kWh' 컬럼을 가진 한 계기분 실측 시계열.

    비non-null 값 전부가 정각(minute==0)이면 '1hour', 아니면 '15min'.
    표본이 min_samples 미만이면(A-L-84처럼 사실상 데이터가 거의 없는 계기) 공집합에서
    "전부 정각"이 공허하게 참이 되는 함정을 막기 위해 무조건 '15min'(보수적 기본값)을 반환한다.
    """
    observed = meter_ts.dropna(subset=["recv_kWh"])
    if len(observed) < min_samples:
        return "15min"
    if (observed["time"].dt.minute == 0).all():
        return "1hour"
    return "15min"


def _apparent_power(meter_ts: pd.DataFrame) -> pd.Series:
    """상별 V×I 합. 비율로만 쓰므로 1/1000·1/√3 같은 상수배는 생략한다."""
    phases = [meter_ts[v] * meter_ts[i] for v, i in (("V_A", "I_a"), ("V_B", "I_b"), ("V_C", "I_c"))]
    return pd.concat(phases, axis=1).sum(axis=1, min_count=1)


def split_hourly_energy(meter_ts: pd.DataFrame) -> pd.DataFrame:
    """
    한 계기(1시간 적산)의 원천 행 -> 정각 적산값을 15분 구간 4개로 나눈 행.

    - 구간 몫 = 그 구간의 V×I ÷ 4개 구간 V×I 합. V×I가 빠진 구간은 같은 시간의 나머지 평균으로
      채우고, 4개 모두 없거나 합이 0이면 4등분한다.
    - 원천에 행이 없던 구간은 새로 만든다(전압·전류는 NULL).
    - 데이터 시작 이전 구간(첫 정각의 앞 45분)은 몫만 계산에 반영하고 행은 버린다.

    반환: 입력과 같은 컬럼 + is_redistributed(분해된 구간 True).
    """
    g = meter_ts.set_index("time").sort_index()
    hours = g.index[(g.index.minute == 0) & g["recv_kWh"].notna()]

    slots = pd.DataFrame({
        "time": np.concatenate([(hours - off).to_numpy() for off in _HOUR_WINDOW]),
        "T": np.tile(hours.to_numpy(), len(_HOUR_WINDOW)),
    })
    power = pd.Series(_apparent_power(g).reindex(slots["time"]).to_numpy(), index=slots.index)
    power = power.fillna(power.groupby(slots["T"]).transform("mean"))
    total = power.groupby(slots["T"]).transform("sum")
    share = (power / total).where(total > 0, 0.25).fillna(0.25).to_numpy()

    for col in RECV_ENERGY_COLS:
        slots[col] = g[col].reindex(slots["T"]).to_numpy() * share
    slots = slots[slots["time"] >= g.index.min()]

    idx = pd.DatetimeIndex(slots["time"])
    out = g.reindex(g.index.union(idx))
    out.loc[idx, RECV_ENERGY_COLS] = slots[RECV_ENERGY_COLS].to_numpy()
    out["meter_id"] = g["meter_id"].iloc[0]
    if "선로명" in out.columns:
        out["선로명"] = g["선로명"].iloc[0]
    out["is_redistributed"] = out.index.isin(idx)
    return out.rename_axis("time").reset_index()


def load_real_timeseries(timeseries_pkl: Path, meter_ids: Iterable[str] | None = None) -> pd.DataFrame:
    """
    visualize_analyis_data/output/timeseries_clean.pkl을 읽어, 1시간 적산 계기는 15분으로
    분해해 돌려준다(원본 pkl 컬럼 + is_redistributed). 원천 pkl을 읽는 모든 단계의 단일 진입점.
    """
    ts = pd.read_pickle(timeseries_pkl)
    if meter_ids is not None:
        ts = ts[ts["meter_id"].isin(set(meter_ids))]

    parts = []
    for _, g in ts.groupby("meter_id", sort=False):
        if detect_data_resolution(g) == "1hour":
            parts.append(split_hourly_energy(g))
        else:
            parts.append(g.assign(is_redistributed=False))
    if not parts:
        return ts.assign(is_redistributed=False)
    return pd.concat(parts, ignore_index=True).sort_values(["meter_id", "time"], ignore_index=True)
