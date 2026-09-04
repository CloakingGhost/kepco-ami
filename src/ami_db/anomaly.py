# -*- coding: utf-8 -*-
"""
안전감지 위기 감지(2-7단계, 프로젝트 2순위 기능).

## 두 등급의 정의 - 이게 설계의 출발점이다

  - **위험** = 실제로 전기사고가 발생한(발생 중인) 상황. 즉시 조치 대상.
  - **주의** = 아직 사고는 아니지만 사고가 나기에 충분한 조건. 점검·관리 대상.

## 왜 통계적 이상치를 등급에 직접 매핑하면 안 되는가

초기 구현은 Tukey 이상치 판정(1.5xIQR=주의, 3xIQR=위험)을 그대로 등급으로 썼다.
그 결과 실측 3개월 x 21개 매장에서 위험 4,111건 / 주의 11,656건이 나왔다 - 평균
50분에 한 번씩 전기사고가 났다는 뜻이 되어 위 정의와 정면으로 어긋난다.

원인은 명확하다. **"통계적으로 드문 값"과 "물리적으로 위험한 값"은 다른 개념이다.**
냉장고 제상 사이클, 심야 청소, 예약 취사 같은 지극히 정상적인 활동도 그 매장의
평소 야간 분포에서 보면 3xIQR을 쉽게 넘긴다. 반대로 계약전력의 1.5배를 빨아쓰는
진짜 과부하 상태라도, 그 매장이 원래 변동이 심한 계기라면 통계적으로는 3xIQR
안쪽에 들어와 안 잡힐 수 있다. 통계는 "평소와 다른가"를 답하지 "위험한가"를 답하지
않는다.

## 그래서 등급 판정은 전부 "전기설비 절대 기준 + 지속시간"으로 간다

세 규칙 모두 (1) 계약전력 대비 절대 비율과 (2) 그 상태가 유지된 시간을 함께 본다.
지속시간을 조건으로 두는 이유는 물리 그 자체다 - 전선 발열은 순간값이 아니라 열의
누적으로 일어나므로, 전기설비 기준들도 전부 "얼마를 얼마나 오래"의 쌍으로 규정돼
있다. 슬롯 하나 튄 값은 노이즈이고, 같은 수준이 몇 시간 유지되는 것이 사고다.

  [위험] RULE_DANGER_OVERLOAD
      계약전력의 145%가 60분(15분 슬롯 4개) 연속 지속.
      근거: KEC(한국전기설비규정) 212 과전류 보호 - 과부하 보호장치의 규약동작전류
      (I2)는 케이블 허용전류(IZ)의 1.45배 이하로 하고, 1.45xIZ가 60분간 지속되면
      도체가 연속사용온도(위험온도)에 도달한다. 즉 이 조건이 성립한 시점에 전선은
      이미 설계 한계를 넘긴 온도에 도달해 있다 - "곧 위험해진다"가 아니라 "이미
      사고 조건에 들어갔다".

  [주의] RULE_CONTINUOUS_LOAD
      계약전력의 80%가 3시간(슬롯 12개) 연속 지속.
      근거: 연속부하(continuous load) 80% 규칙 - NEC 210.20(A) 및 Article 100
      정의("최대 전류가 3시간 이상 지속되는 부하"). 과전류 보호장치는 연속부하의
      125% 이상으로 정격을 잡아야 하고, 뒤집으면 연속부하는 정격의 80%를 넘으면
      안 된다. 이 선을 넘었다는 건 사고는 아직 아니지만 설비가 안전 설계 범위
      밖에서 돌고 있다는 뜻 - 정확히 "사고가 나기에 충분한 조건"이다.
      (이 80%/3시간 규칙은 NEC 조문으로 확인한 것이고, 대응하는 KEC 조문 번호까지는
       확인하지 못했으므로 KEC 조문으로 인용하지 않는다.)

  [주의] RULE_PATTERN_DEVIATION
      그 매장 자신의 (요일 x 15분슬롯) 중앙값에서 3xfloor 이상 벗어나면서
      **동시에** 계약전력의 50% 이상을, 60분(슬롯 4개) 연속 지속.
      근거: 위 두 규칙은 계약전력이라는 명판값만 보므로 "문 닫은 가게에서 뭔가
      계속 돌아가고 있다"를 놓친다(계약 45kW 매장이 새벽에 20kW를 쓰면 절대
      기준으론 55%라 안 걸리지만, 그 매장 평소 새벽 1kW 대비론 명백한 이상이다).
      이 규칙이 매장별 학습 패턴을 쓰는 유일한 자리다. 단, 패턴 이탈만으로는
      등급을 주지 않고 **계약전력 50% 이상**이라는 절대 하한을 AND로 걸었다 -
      이 하한이 없으면 야간 사용량이 0에 수렴하는 매장(A-L-37 등)에서 소형가전
      하나 켠 수준(0.04kWh)까지 이상으로 잡히는 퇴화 문제가 그대로 재발한다.

## 실측 검증 (2026-04-01~06-30, 21개 매장, closed_hours)

  위험 0건 / 주의 26건(2개 매장). 실측 3개월간 실제 전기사고는 없었으므로 위험
  0건이 정답이고, 주의 26건은 매장당 3개월에 1.2건꼴이라 사람이 실제로 확인할 수
  있는 양이다. 위 재설계 전에는 같은 구간에서 위험 2,107건 / 주의 5,272건이었다.

## 판정 범위

  closed_hours(영업시간표상 문이 닫혀 있어야 하는 슬롯)만 대상으로 한다
  (data/프로젝트개요.md "영업종료 이후 이상치 감지"). 영업 중 과부하도 물리적으로는
  똑같이 위험하지만, 지금 데이터로는 "바쁜 정상 영업"과 "위험한 과부하"를 가를
  방법이 없다 - 실제로 A-L-58은 영업시간 중 계약전력의 145%를 넘긴 슬롯이 실측
  구간에만 2,866개나 되는데(중앙값 이용률 자체가 106%), 이건 사고가 아니라 그
  계기의 contract_power_kw가 실제 사용량과 맞지 않는 데이터 품질 문제로 보인다.
  이 문제가 정리되기 전에 영업시간대로 판정을 넓히면 오탐이 폭증한다.

## 향후 ML/DL 확장 지점

  RULE_PATTERN_DEVIATION이 매장별 학습 baseline을 쓰는 자리이므로, 이상탐지·추세
  예측 모델은 이 규칙을 대체/보강하는 형태로 들어오는 게 자연스럽다(지금의
  median/IQR은 "현재 슬롯이 정상 범위 밖인가"만 보고 추세를 못 본다 - "최근 N일간
  야간 baseline이 서서히 올라가고 있다"처럼 임계치를 넘기 전에 미리 예측하는 방향).
  반대로 RULE_DANGER_OVERLOAD는 안전계 하드 트립와이어라 확률 모델로 대체하지
  않는다 - 실제 배선용차단기가 확률이 아니라 결정론적 규칙으로 동작하는 것과 같은
  이유다. ML/DL 자체는 이번 범위 밖이다(정답 라벨이 없어 성능 검증이 불가능 -
  visualize_analyis_data/docs/05_시각분석_AI방법론_보고서.md 122~123행과 동일한 이유).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# --- 위험: KEC 212 (계약전력 145%, 60분 지속) ---
DANGER_OVERLOAD_RATIO = 1.45
DANGER_SUSTAINED_SLOTS = 4          # 60분 = 15분 x 4
RULE_DANGER_OVERLOAD = "kec212_overload_145pct_60min"

# --- 주의: 연속부하 80% 규칙 (NEC 210.20(A), 3시간 지속) ---
CONTINUOUS_LOAD_RATIO = 0.80
CONTINUOUS_SUSTAINED_SLOTS = 12     # 3시간 = 15분 x 12
RULE_CONTINUOUS_LOAD = "continuous_load_80pct_180min"

# --- 주의: 매장 패턴 이탈 + 절대 하한 (60분 지속) ---
PATTERN_DEVIATION_MULT = 3.0        # Tukey(1977) outer fence
PATTERN_MIN_LOAD_RATIO = 0.50       # 계약전력 대비 절대 하한(퇴화 매장 오탐 차단)
PATTERN_SUSTAINED_SLOTS = 4
RULE_PATTERN_DEVIATION = "pattern_deviation_3iqr_50pct_60min"

EVENT_COLUMNS = ["ts", "recv_kWh", "level", "rule_triggered", "threshold_value"]


def compute_group_thresholds(meter_ts: pd.DataFrame, min_deviation_floor_frac: float = 0.1) -> pd.DataFrame:
    """
    실측 시계열(한 계기분, 'time'/'recv_kWh' 컬럼) -> (요일, 슬롯)별 median/IQR/floor.
    floor = max(IQR, 중앙값의 min_deviation_floor_frac 비율) - IQR이 0에 가까워지는
    저변동 계기에서 아주 미세한 변동까지 전부 이상치로 잡히는 걸 방지.

    (요일, 슬롯) 672개 조합 중 일부는 실측 3개월 안에 유효한 관측치가 아예 없을 수
    있다(결측 구간이 하필 그 요일x시간대와 겹치는 경우) - 이런 조합은 groupby 결과에
    median=NaN으로 남고, 그대로 두면 병합 시 그 슬롯은 항상 "정상" 취급되어 아무리
    극단적인 값이 와도 절대 이상치로 잡히지 않는 사각지대가 생긴다(실측으로 A-L-16의
    화요일 02:30 슬롯에서 이 문제를 확인함 - 주입한 mock 스파이크가 조용히
    누락됐었음). 그래서 데이터가 없는 조합은 "계기 전체(모든 요일x시간대 통합)"
    median/IQR로 폴백시켜, 최소한의 이상치 탐지 능력을 보장한다.

    RULE_PATTERN_DEVIATION 전용이다 - 위험/주의 등급을 이 값만으로 정하지는 않는다
    (모듈 docstring "왜 통계적 이상치를 등급에 직접 매핑하면 안 되는가" 참고).
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

    full_index = pd.MultiIndex.from_product([range(7), range(96)], names=["dow", "slot"])
    grouped = grouped.set_index(["dow", "slot"]).reindex(full_index).reset_index()
    grouped["median"] = grouped["median"].fillna(overall_median)
    grouped["iqr"] = grouped["iqr"].fillna(overall_iqr)

    grouped["floor"] = np.maximum(grouped["iqr"], min_deviation_floor_frac * grouped["median"].abs())
    grouped["floor"] = grouped["floor"].clip(lower=0.01)  # 완전한 0 편차 방지(중앙값 자체가 0인 극단 케이스)
    return grouped[["dow", "slot", "median", "floor"]]


def sustained_mask(ts: pd.Series, condition: pd.Series, slots: int) -> pd.Series:
    """
    condition이 연속 slots개 이상 유지된 구간에 속하는 행을 True로 표시한다.

    closed_hours로 미리 걸러진 입력은 영업시간대가 빠져있어 ts가 항상 15분 간격으로
    연속이라는 보장이 없다(어젯밤 마지막 폐점 슬롯 -> 오늘 첫 폐점 슬롯 사이에
    영업시간 전체가 빠짐). ts 간격이 정확히 900초인 구간만 "연속"으로 인정해서,
    끊긴 두 구간을 하나의 지속 구간으로 잘못 잇는 것을 막는다.

    윈도우 끝 슬롯만이 아니라 그 윈도우에 속한 slots개 행을 전부 True로 만든다 -
    이 프로젝트의 "모든 판정은 슬롯 단위" 관례를 지키기 위함.
    """
    cond = pd.Series(condition).reset_index(drop=True).fillna(False).astype(bool)
    gap_ok = pd.Series(ts).reset_index(drop=True).diff().dt.total_seconds().eq(900.0)

    window_end = (cond.rolling(slots).sum() == slots) & (gap_ok.rolling(slots - 1).sum() == slots - 1)
    member = pd.Series(False, index=cond.index)
    for lead in range(slots):
        member = member | window_end.shift(-lead).fillna(False)
    return member


def detect_events(
    full_ts: pd.DataFrame, thresholds: pd.DataFrame, contract_power_kw: float | None,
) -> pd.DataFrame:
    """
    full_ts: 'ts'/'recv_kWh' 컬럼. closed_hours로 이미 필터된 시계열을 넘기는 게
             정상 사용법이다(필터는 호출부 책임 - 10_detect_anomalies.py).
    thresholds: compute_group_thresholds() 결과.
    contract_power_kw: 그 계기의 계약전력. 세 규칙 전부 이 값을 기준으로 하므로
                       없으면(NULL/0) 판정 자체가 불가능해 빈 결과를 반환한다.

    반환: 플래그된 행만 EVENT_COLUMNS로. 등급 우선순위는 위험 > 주의이고,
    주의끼리는 연속부하 규칙 > 패턴 이탈 규칙 순으로 rule_triggered를 정한다
    (한 슬롯이 여러 규칙에 동시에 걸릴 수 있는데 anomaly_events.rule_triggered는
    단일 값이라, 더 물리적 근거가 직접적인 쪽을 남긴다).
    """
    df = full_ts.copy().sort_values("ts").reset_index(drop=True)
    if df.empty or contract_power_kw is None or pd.isna(contract_power_kw) or contract_power_kw <= 0:
        return pd.DataFrame(columns=EVENT_COLUMNS)

    df["dow"] = df["ts"].dt.dayofweek
    df["slot"] = df["ts"].dt.hour * 4 + df["ts"].dt.minute // 15
    m = df.merge(thresholds, on=["dow", "slot"], how="left")

    # 15분 kWh를 순간 kW로 환산(x4)해 계약전력과 같은 단위로 비교한다.
    load_ratio = m["recv_kWh"] * 4 / contract_power_kw

    danger_threshold = contract_power_kw * DANGER_OVERLOAD_RATIO / 4
    continuous_threshold = contract_power_kw * CONTINUOUS_LOAD_RATIO / 4
    pattern_threshold = m["median"] + PATTERN_DEVIATION_MULT * m["floor"]

    is_danger = sustained_mask(
        m["ts"], load_ratio >= DANGER_OVERLOAD_RATIO, DANGER_SUSTAINED_SLOTS
    )
    is_continuous = sustained_mask(
        m["ts"], load_ratio >= CONTINUOUS_LOAD_RATIO, CONTINUOUS_SUSTAINED_SLOTS
    )

    deviation = (m["recv_kWh"] - m["median"]).abs()
    pattern_hit = (deviation >= PATTERN_DEVIATION_MULT * m["floor"]) & (load_ratio >= PATTERN_MIN_LOAD_RATIO)
    is_pattern = sustained_mask(m["ts"], pattern_hit, PATTERN_SUSTAINED_SLOTS)

    level = np.where(is_danger, "위험", np.where(is_continuous | is_pattern, "주의", None))
    rule = np.where(
        is_danger, RULE_DANGER_OVERLOAD,
        np.where(is_continuous, RULE_CONTINUOUS_LOAD,
                 np.where(is_pattern, RULE_PATTERN_DEVIATION, None)),
    )
    threshold_value = np.where(
        is_danger, danger_threshold,
        np.where(is_continuous, continuous_threshold,
                 np.where(is_pattern, pattern_threshold, np.nan)),
    )

    out = m.assign(level=level, rule_triggered=rule, threshold_value=threshold_value)
    return out.loc[out["level"].notna(), EVENT_COLUMNS].reset_index(drop=True)
