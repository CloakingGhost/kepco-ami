# -*- coding: utf-8 -*-
"""
1시간 해상도 계기(recv_kWh가 매시 정각에만 존재) 탐지 + 한식 코호트 기반 재분배.

배경(직접 데이터로 확인): 화곡동 매칭 21개 중 5개 계기(A-L-16/19/49/58/70)는
recv_kWh가 매시 정각에만 찍히고 15/30/45분은 91일 전체 예외 0건으로 항상 결측이다
(무작위 결측이 아니라 계기 통신 사양 차이). 억지로 15분 정밀도를 만들어내지 않고
해상도 차이를 투명하게 드러내는 게 기본 방침(meters.data_resolution 플래그)이지만,
한식당(A-L-58)은 20~30분 회전 주기가 있어 1시간 해상도로는 혼잡도 표현이 너무
거칠다 - 이 계기 하나에 한해 같은 업종(한식) 코호트의 시간 내 상대 형태를 정각
실측값에 앵커링해 15/30/45분을 추정 보충한다("시간합계 4등분"이 아니라 "정각값을
기준점 삼아 코호트 상대 비율을 곱하는" 방식 - 정각값의 크기가 이웃 매장 15분값과
비슷한 스케일이라 "그 15분 사용량 중 1개만 리포트되고 나머지는 유실"로 해석하는 게
맞다는 게 실측으로 확인됨).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

MIN_COHORT_SIZE = 3  # 재분배용 코호트(같은 업종, 15min 정상 해상도) 최소 계기 수 - 미만이면 재분배 포기


def cohort_meter_ids(
    matched: pd.DataFrame,
    resolution_by_meter: dict[str, str],
    target_meter_id: str,
    biz_mid,
    biz_large,
) -> list[str]:
    """
    target_meter_id와 같은 업종(중분류 우선, 부족하면 대분류로 확대)이면서 15min 정상
    해상도인 다른 matched 계기 id 목록. 02(실측 적재)/06(합성 생성) 양쪽이 각자 실행
    컨텍스트에서 재분배를 다시 계산할 때 동일한 코호트 선정 규칙을 쓰도록 공유한다.

    matched: meter_id/biz_category_mid/biz_category_large 컬럼을 가진 DataFrame.
    중분류 코호트가 MIN_COHORT_SIZE 이상이면 그걸 그대로 쓰고, 부족하면 대분류로
    확대한 결과를 반환한다(그래도 부족하면 호출부가 재분배를 포기 - len() < MIN_COHORT_SIZE 체크는
    호출부 책임).
    """
    def eligible(mask) -> list[str]:
        cand = matched.loc[mask & (matched["meter_id"] != target_meter_id), "meter_id"]
        return [m for m in cand if resolution_by_meter.get(m) == "15min"]

    mid_cohort = eligible(matched["biz_category_mid"] == biz_mid)
    if len(mid_cohort) >= MIN_COHORT_SIZE:
        return mid_cohort
    return eligible(matched["biz_category_large"] == biz_large)


def detect_data_resolution(meter_ts: pd.DataFrame, min_samples: int = 100) -> str:
    """
    meter_ts: 'time'/'recv_kWh' 컬럼을 가진 한 계기분 실측 시계열(synthetic.build_slot_profile과
    동일 스키마 관례).

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


def build_hourly_ratio_table(cohort_ts: pd.DataFrame) -> dict[int, tuple[float, float, float, float]]:
    """
    cohort_ts: 15분 정상 해상도인 코호트 계기들을 합친 'time'/'recv_kWh' 시계열.

    (hour, minute)로 그룹핑한 평균을 구하고, 정각(minute=0) 평균 대비 상대 비율로
    정규화한다. 정각 평균이 0/NaN이거나 15/30/45분 중 하나라도 그 시간대 표본이
    없는 시간대는 결과에서 제외한다(재분배 기준점 자체가 무의미하므로).

    반환: {hour: (1.0, ratio_15, ratio_30, ratio_45)}
    """
    df = cohort_ts.dropna(subset=["recv_kWh"]).copy()
    df["hour"] = df["time"].dt.hour
    df["minute"] = df["time"].dt.minute
    means = df.groupby(["hour", "minute"])["recv_kWh"].mean()

    ratios: dict[int, tuple[float, float, float, float]] = {}
    for hour in range(24):
        if (hour, 0) not in means.index:
            continue
        mean_00 = means.loc[(hour, 0)]
        if pd.isna(mean_00) or mean_00 == 0:
            continue

        values: list[float] = []
        for minute in (0, 15, 30, 45):
            if (hour, minute) not in means.index or pd.isna(means.loc[(hour, minute)]):
                values = []
                break
            values.append(float(means.loc[(hour, minute)]) / float(mean_00))
        if values:
            ratios[hour] = tuple(values)  # type: ignore[assignment]

    return ratios


def redistribute_hourly_store(
    target_ts: pd.DataFrame, ratios: dict[int, tuple[float, float, float, float]]
) -> pd.DataFrame:
    """
    target_ts: 'time'/'recv_kWh' 컬럼의 1시간 해상도 계기 데이터(정각만 실측 존재).

    정각 실측이 있는 각 시간에 대해 15/30/45분 슬롯 3개를 원래 존재 여부와 무관하게
    항상 새로 구성한다(NULL로 존재하던 행이든, 행 자체가 아예 없던 케이스든 이 함수
    안에서는 구분하지 않고 동일하게 취급 - 호출부가 반환값을 그대로 insert 대상에
    합류시키면 된다). 정각 행은 is_redistributed=False로 원값 그대로 보존하고,
    새로 만든 15/30/45분 행은 is_redistributed=True로 표시한다. 코호트 비율표에
    없는 시간대(그 시간대 표본이 코호트에도 부족)는 정각값만 남기고 보충하지 않는다.

    반환 컬럼: time, recv_kWh, is_redistributed (다른 계측 컬럼(전압/전류 등)은 이 함수의
    관심사가 아니다 - 새로 만든 행에 대해 그 값들을 지어내지 않고 NULL로 두는 처리는
    호출부에서 한다).
    """
    hourly = target_ts.dropna(subset=["recv_kWh"])
    hourly = hourly[hourly["time"].dt.minute == 0]

    rows = []
    for _, r in hourly.iterrows():
        hour_ts = r["time"]
        rows.append({"time": hour_ts, "recv_kWh": float(r["recv_kWh"]), "is_redistributed": False})

        hour_ratios = ratios.get(hour_ts.hour)
        if hour_ratios is None:
            continue
        _, r15, r30, r45 = hour_ratios
        for minute, ratio in ((15, r15), (30, r30), (45, r45)):
            rows.append({
                "time": hour_ts + pd.Timedelta(minutes=minute),
                "recv_kWh": float(r["recv_kWh"]) * ratio,
                "is_redistributed": True,
            })

    return pd.DataFrame(rows, columns=["time", "recv_kWh", "is_redistributed"]).sort_values("time").reset_index(drop=True)


def write_redistribution_report(
    path: Path,
    source_script: str,
    applied: dict[str, dict],
    skipped: dict[str, str],
) -> None:
    """
    02/06단계가 실제로 계산한 코호트 비율표를 감사(audit) 가능하게 파일로 남긴다.

    이 비율표는 그 자체로는 DB 어디에도 저장되지 않는다(02와 06이 각자 재계산해서
    바로 소비하고 버리는 게 원래 설계 - 09가 pkl을 독립적으로 재읽어 night_baseline을
    다시 계산하는 것과 동일한 패턴). "왜 이 값이 나왔는지"를 나중에 다시 계산 없이
    확인하려면 이 리포트가 유일한 창구이므로, 02/06 둘 다 실행할 때마다 자기 몫을 남긴다.

    applied: {meter_id: {"biz_category_mid":.., "biz_category_large":.., "cohort_meter_ids":[..],
                          "hourly_ratios": {hour: [1.0, r15, r30, r45]}, "redistributed_row_count":..}}
    skipped: {meter_id: "코호트 부족(N개)" 같은 사유 문자열} - data_resolution='1hour'인데 재분배를
             적용하지 않은 계기들 (해상도 플래그만 남은 이유를 추적할 수 있게 함)
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_script": source_script,
        "min_cohort_size": MIN_COHORT_SIZE,
        "applied": applied,
        "skipped": skipped,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
