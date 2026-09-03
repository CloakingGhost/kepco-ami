# -*- coding: utf-8 -*-
"""
안전감지 이상치 탐지(2-7단계, 프로젝트 2순위 기능).

정규분포 가정(z-score) 대신 시간대x요일 그룹별 median+IQR 규칙을 쓰는 이유:
visualize_analyis_data/docs/05_시각분석_AI방법론_보고서.md 4-1절에서 대표 계기
4개 전부 정규분포 가설이 기각됐고(p<<0.05), 4-3절에서 저사용/저변동 계기(A-L-37
사례)에 전역 IQR을 그대로 적용하면 미세한 변동까지 전부 이상치로 잡히는 함정이
실측으로 확인됐다. min_deviation_floor_frac으로 "최소 편차 바닥"을 둬서
이 함정을 막는다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def compute_group_thresholds(meter_ts: pd.DataFrame, min_deviation_floor_frac: float = 0.1) -> pd.DataFrame:
    """
    실측 시계열(한 계기분, 'time'/'recv_kWh' 컬럼) -> (요일, 슬롯)별 median/IQR/floor.
    floor = max(IQR, 중앙값의 min_deviation_floor_frac 비율) - IQR이 0에 가까워지는
    저변동 계기에서 아주 미세한 변동까지 전부 이상치로 잡히는 걸 방지.

    (요일, 슬롯) 672개 조합 중 일부는 실측 3개월 안에 유효한 관측치가 아예 없을 수
    있다(결측 구간이 하필 그 요일x시간대와 겹치는 경우) - 이런 조합은 groupby 결과에
    median=NaN으로 남고, 그대로 두면 병합(flag_anomalies) 시 그 슬롯은 항상 "정상"
    취급되어 아무리 극단적인 값이 와도 절대 이상치로 잡히지 않는 사각지대가 생긴다
    (실측으로 A-L-16의 화요일 02:30 슬롯에서 이 문제를 확인함 - 주입한 mock 스파이크가
    조용히 누락됐었음). 그래서 데이터가 없는 조합은 "계기 전체(모든 요일x시간대 통합)"
    median/IQR로 폴백시켜, 최소한의 이상치 탐지 능력을 보장한다.
    """
    df = meter_ts.copy()
    df["dow"] = df["time"].dt.dayofweek
    df["slot"] = df["time"].dt.hour * 4 + df["time"].dt.minute // 15

    grouped = (
        df.groupby(["dow", "slot"])["recv_kWh"]
        .agg(median="median", q1=lambda s: s.quantile(0.25), q3=lambda s: s.quantile(0.75))
        .reset_index()
    )
    grouped["iqr"] = grouped["q3"] - grouped["q1"]

    # 계기 전체 통합 폴백 통계 (그룹 데이터가 없을 때만 쓰임)
    overall_median = float(df["recv_kWh"].median())
    overall_iqr = float(df["recv_kWh"].quantile(0.75) - df["recv_kWh"].quantile(0.25))

    # 672개 (dow, slot) 조합 전체로 reindex해서 데이터가 아예 없던 조합도 행으로 만들고,
    # 그 행의 median/iqr을 전체 통합 폴백값으로 채운다.
    full_index = pd.MultiIndex.from_product([range(7), range(96)], names=["dow", "slot"])
    grouped = grouped.set_index(["dow", "slot"]).reindex(full_index).reset_index()
    grouped["median"] = grouped["median"].fillna(overall_median)
    grouped["iqr"] = grouped["iqr"].fillna(overall_iqr)

    grouped["floor"] = np.maximum(grouped["iqr"], min_deviation_floor_frac * grouped["median"].abs())
    grouped["floor"] = grouped["floor"].clip(lower=0.01)  # 완전한 0 편차 방지(중앙값 자체가 0인 극단 케이스)
    return grouped[["dow", "slot", "median", "floor"]]


def flag_anomalies(
    full_ts: pd.DataFrame, thresholds: pd.DataFrame, attention_mult: float = 1.5, danger_mult: float = 3.0,
) -> pd.DataFrame:
    """
    full_ts: 'ts'/'recv_kWh' 컬럼을 가진, 이미 "영업종료 이후"로 필터된 시계열
    (안전감지는 영업시간 중 스파이크가 아니라 종료 이후 이상 신호를 전제로 하므로 -
    data/프로젝트개요.md "영업종료 이후 이상치 감지" 참고. 필터는 호출부 책임).

    반환: level('주의'|'위험'|None), rule_triggered, threshold_value 컬럼이 추가된 DataFrame.
    """
    df = full_ts.copy()
    df["dow"] = df["ts"].dt.dayofweek
    df["slot"] = df["ts"].dt.hour * 4 + df["ts"].dt.minute // 15
    merged = df.merge(thresholds, on=["dow", "slot"], how="left")

    deviation = (merged["recv_kWh"] - merged["median"]).abs()
    danger_gate = danger_mult * merged["floor"]
    attention_gate = attention_mult * merged["floor"]

    merged["level"] = np.select(
        [merged["recv_kWh"].isna(), deviation >= danger_gate, deviation >= attention_gate],
        [None, "위험", "주의"],
        default=None,
    )
    merged["rule_triggered"] = np.where(
        merged["level"] == "위험", f"group_iqr_{danger_mult}x",
        np.where(merged["level"] == "주의", f"group_iqr_{attention_mult}x", None),
    )
    merged["threshold_value"] = np.where(
        merged["level"] == "위험", merged["median"] + danger_gate,
        np.where(merged["level"] == "주의", merged["median"] + attention_gate, None),
    )
    return merged
