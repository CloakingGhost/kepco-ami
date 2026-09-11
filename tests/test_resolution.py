# -*- coding: utf-8 -*-
"""
ami_db.resolution.split_hourly_energy - 1시간 적산값을 15분 구간 4개로 나누는 규칙.

정각 T의 값은 T-45/T-30/T-15/T로 끝나는 구간 4개의 합이다(원천 데이터 검증 근거는
db/docs/시간적산계기_15분분해_분석보고서.md). 그래서 분해 후에도 그 4개 합은 정각값과
정확히 같아야 하고, 몫은 각 구간의 V×I에 비례해야 한다.
"""
import numpy as np
import pandas as pd
import pytest

from ami_db.resolution import detect_data_resolution, split_hourly_energy


def _meter(rows):
    """rows: [(시각, recv_kWh, V_A, I_a)] -> 원천 pkl 스키마를 흉내 낸 단상 계기 한 대분."""
    df = pd.DataFrame(rows, columns=["time", "recv_kWh", "V_A", "I_a"])
    df["time"] = pd.to_datetime(df["time"])
    df["meter_id"] = "A-L-TEST"
    df["recv_kVAh"] = df["recv_kWh"]
    for col in ("recv_lag_kvarh", "recv_lead_kvarh", "V_B", "V_C", "I_b", "I_c"):
        df[col] = np.nan
    return df


def _kwh(out):
    return out.set_index("time")["recv_kWh"]


def test_hour_is_split_by_vi_share_and_total_is_conserved():
    out = split_hourly_energy(_meter([
        ("2026-04-01 00:00", 2.0, 220, 1),
        ("2026-04-01 00:15", np.nan, 220, 1),
        ("2026-04-01 00:30", np.nan, 220, 2),
        ("2026-04-01 00:45", np.nan, 220, 3),
        ("2026-04-01 01:00", 4.0, 220, 2),
    ]))
    kwh = _kwh(out)
    # 01:00의 4.0은 00:15/00:30/00:45/01:00에 V×I(1:2:3:2) 비율로 나뉜다.
    assert kwh["2026-04-01 00:15"] == pytest.approx(0.5)
    assert kwh["2026-04-01 00:30"] == pytest.approx(1.0)
    assert kwh["2026-04-01 00:45"] == pytest.approx(1.5)
    assert kwh["2026-04-01 01:00"] == pytest.approx(1.0)
    assert kwh["2026-04-01 00:15":"2026-04-01 01:00"].sum() == pytest.approx(4.0)
    assert out["is_redistributed"].all()


def test_first_hour_keeps_only_its_own_share_and_drops_slots_before_data_start():
    out = split_hourly_energy(_meter([
        ("2026-04-01 00:00", 2.0, 220, 1),
        ("2026-04-01 01:00", 4.0, 220, 1),
    ]))
    # 00:00의 2.0은 전날 23:15~00:00 구간 몫이라 00:00 자기 몫(1/4)만 남는다.
    assert _kwh(out)["2026-04-01 00:00"] == pytest.approx(0.5)
    assert out["time"].min() == pd.Timestamp("2026-04-01 00:00")


def test_missing_vi_slot_takes_hour_mean_and_missing_row_is_created():
    out = split_hourly_energy(_meter([
        ("2026-04-01 00:00", np.nan, 220, 1),
        ("2026-04-01 00:15", np.nan, 220, 1),
        ("2026-04-01 00:45", np.nan, 220, 1),
        ("2026-04-01 01:00", 3.0, 220, 1),
    ]))
    kwh = _kwh(out)
    assert kwh["2026-04-01 00:30"] == pytest.approx(0.75)  # 원천에 없던 행 - 같은 시간 평균 V×I로 몫 계산
    assert pd.isna(out.set_index("time").loc["2026-04-01 00:30", "V_A"])
    # 정각값이 없던 00:00은 분해 대상이 아니다.
    assert pd.isna(kwh["2026-04-01 00:00"])
    assert not out.set_index("time").loc["2026-04-01 00:00", "is_redistributed"]


def test_meter_without_any_vi_is_split_evenly():
    kwh = _kwh(split_hourly_energy(_meter([
        ("2026-04-01 00:00", 1.0, np.nan, np.nan),
        ("2026-04-01 01:00", 2.0, np.nan, np.nan),
    ])))
    for t in ("00:15", "00:30", "00:45", "01:00"):
        assert kwh[f"2026-04-01 {t}"] == pytest.approx(0.5)


def test_resolution_flips_from_1hour_to_15min_after_split():
    times = pd.date_range("2026-04-01", periods=24 * 5, freq="h")
    meter = _meter([(t, 1.0, 220, 1) for t in times])
    assert detect_data_resolution(meter) == "1hour"
    assert detect_data_resolution(split_hourly_energy(meter)) == "15min"
