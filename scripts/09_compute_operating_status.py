# -*- coding: utf-8 -*-
"""
2-6단계: store_operating_status를 계기 x 시각(실측+합성 전체 그리드)에 대해 채운다.

night_baseline/혼잡도 Q1·Q3는 "실측 데이터만"으로 계산한다 - 계기의 진짜 습성을
보여주는 기준이어야 하는데, 합성 데이터 자체가 이 실측 통계에서 리샘플링된
것이라 합성까지 포함해 기준을 잡으면 자기순환(그 계기가 원래 실측에서 보인
패턴을 다시 기준으로 삼는 것뿐이라 상관없어 보이지만, 굳이 섞을 이유가 없고
"기준은 항상 실측"이라는 원칙을 지키는 편이 설명하기 쉽다) 문제만 생긴다.
합성 7월은 특히 안전감지 데모용으로 위험/주의 이상치가 일부러 심긴 구간이라,
혼잡도 기준 산정에 섞으면 기준 자체가 이상치에 오염된다.
판정 자체는 실측+합성 전체 그리드(meter_timeseries 전체)에 대해 수행한다 -
서빙 로직이 날짜와 무관하게 항상 상태를 보여줄 수 있어야 하기 때문.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ami_db import resolution  # noqa: E402
from ami_db.config import VIZ_OUTPUT, settings  # noqa: E402
from ami_db.db import bulk_insert, get_raw_connection  # noqa: E402
from ami_db.status import (  # noqa: E402
    FINAL_STATUS_MATRIX,
    compute_congestion_thresholds,
    load_effective_hours,
)
from ami_db.synthetic import compute_night_baseline  # noqa: E402

ACTIVE_RATIO_THRESHOLD = 1.5  # night_baseline 대비 몇 배 이상이어야 "가동중"으로 볼지


def _sod(t) -> float:
    """datetime.time -> 자정 이후 초(seconds of day). None이면 NaN."""
    if t is None:
        return np.nan
    return t.hour * 3600 + t.minute * 60 + t.second


def compute_status_for_store(
    meter_ts: pd.DataFrame, day_hours: dict[int, dict], night_baseline: float,
    data_resolution: str = "15min",
) -> pd.DataFrame:
    """
    한 계기(=한 상가)의 전체 시계열(실측+합성)에 대해 벡터화 연산으로
    schedule/power/final_status와 congestion_level을 한 번에 계산한다.

    meter_ts는 ts/recv_kWh 외에 is_synthetic 컬럼도 있어야 한다(혼잡도 기준을
    실측 구간에서만 뽑기 위함 - ami_db.status.compute_congestion_thresholds 참고).

    data_resolution='1hour'인 계기는 recv_kWh가 NaN인 슬롯(전체의 75%, 구조적 결측)을
    판정 대상에서 아예 제외한다 - determine_power_status의 "결측이면 low로 방어 처리"
    안전장치는 "가끔의 결측"을 가정한 설계라, 상시·구조적 결측에 그대로 적용하면 점심
    피크 시간에도 데이터가 없다는 이유만으로 항상 "휴무추정/영업종료"로 왜곡된다(실측으로
    확인함). store_operating_status의 CHECK 제약상 NULL power_status를 허용하지 않으므로,
    "행은 만들되 NULL"이 아니라 "그 슬롯의 판정 자체를 생략(호출부가 insert 안 함)"으로
    처리한다 - 15min 계기는 진짜 가끔의 결측이라 기존 안전장치를 그대로 둔다.
    """
    df = meter_ts.copy()
    if data_resolution == "1hour":
        df = df[df["recv_kWh"].notna()].copy()
    dow = df["ts"].dt.dayofweek

    open_sod_by_dow = {d: _sod(h.get("open_time")) for d, h in day_hours.items()}
    close_sod_by_dow = {d: _sod(h.get("close_time")) for d, h in day_hours.items()}
    is_closed_by_dow = {d: bool(h.get("is_closed")) for d, h in day_hours.items()}
    is_24h_by_dow = {d: bool(h.get("is_24h")) for d, h in day_hours.items()}

    open_sod = dow.map(open_sod_by_dow)
    close_sod = dow.map(close_sod_by_dow)
    is_closed_day = dow.map(is_closed_by_dow).fillna(True)
    is_24h_day = dow.map(is_24h_by_dow).fillna(False)
    sod = df["ts"].dt.hour * 3600 + df["ts"].dt.minute * 60 + df["ts"].dt.second

    # 자정을 넘기는 영업시간(예: 17:00~익일 04:00, "25센치꼬치앤오뎅바" 등 실측으로 확인됨) 처리.
    # close_sod < open_sod면 "자정을 넘긴다"고 보고 open 이후이거나 close 이전 둘 중 하나면
    # 영업시간으로 판정한다(status.determine_schedule_status의 스칼라 버전과 동일 규칙 -
    # 이 데이터셋은 요일별 영업시간이 균일해서 "전날 세션이 오늘 새벽까지 이어지는" 케이스를
    # "오늘 자신의 영업시간" 체크만으로도 정확히 잡아낸다).
    crosses_midnight = close_sod < open_sod
    within_same_day = (sod >= open_sod) & (sod <= close_sod)
    within_overnight = (sod >= open_sod) | (sod <= close_sod)
    within_hours = open_sod.notna() & close_sod.notna() & np.where(crosses_midnight, within_overnight, within_same_day)
    df["schedule_status"] = np.select(
        [is_closed_day, is_24h_day, within_hours],
        ["closed_hours", "open_hours", "open_hours"],
        default="closed_hours",
    )

    floor = max(night_baseline, 0.01)
    df["power_status"] = np.where(
        df["recv_kWh"].isna(), "low",
        np.where(df["recv_kWh"] >= floor * ACTIVE_RATIO_THRESHOLD, "active", "low"),
    )

    df["final_status"] = [
        FINAL_STATUS_MATRIX[(s, p)] for s, p in zip(df["schedule_status"], df["power_status"])
    ]

    # 혼잡도 기준은 "그 매장 자신의 동시간대 분포"에서 뽑는다(db/problem/문제상황-1.txt:
    # "여유/보통/혼잡 기준이 불명확하다 - 운영시간을 분포도로, 동시간 최대 1달을 기준으로").
    # 실측(is_synthetic=False) + 영업중 슬롯만 넘겨야 기준이 안전감지용 이상치(합성 7월)에
    # 오염되지 않는다. compute_congestion_thresholds()가 최근 30일·±30분 묶음으로
    # 시간대별 Q1/Q3를 계산한다 - 시간대 구분 없이 영업중 전체를 한 덩어리로 섞던 예전
    # 방식(compute_utilization_quartiles, 삭제됨)은 개점 직후 한산한 시간과 점심 피크가
    # 같은 기준에 뭉뚱그려지는 문제가 있었다.
    real_open = df.loc[(~df["is_synthetic"].astype(bool)) & (df["final_status"] == "영업중"), ["ts", "recv_kWh"]]
    thresholds, fallback = compute_congestion_thresholds(real_open)

    tod = (df["ts"].dt.hour * 60 + df["ts"].dt.minute).astype(int)
    default_pair = fallback if fallback is not None else (np.nan, np.nan)
    q1_by_slot = {slot: thresholds.get(slot, default_pair)[0] for slot in tod.unique()}
    q3_by_slot = {slot: thresholds.get(slot, default_pair)[1] for slot in tod.unique()}
    q1_arr = tod.map(q1_by_slot)
    q3_arr = tod.map(q3_by_slot)

    congestion = np.select(
        [df["recv_kWh"].fillna(0) <= q1_arr, df["recv_kWh"].fillna(0) <= q3_arr], ["하", "중"], default="상"
    )
    df["congestion_level"] = np.where(df["final_status"] == "영업중", congestion, None)

    return df


def main() -> None:
    # synthetic.compute_night_baseline()은 'time' 컬럼명을 기대한다(build_slot_profile과
    # 동일 관례 유지 - 06_generate_synthetic_timeseries.py에서 쓰는 것과 같은 원본 pkl 스키마).
    real_ts_all = resolution.load_real_timeseries(VIZ_OUTPUT / "timeseries_clean.pkl")[["meter_id", "time", "recv_kWh"]]

    with get_raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.store_id, s.meter_id, m.data_resolution "
                "FROM stores s JOIN meters m ON m.meter_id = s.meter_id ORDER BY s.store_id"
            )
            stores = cur.fetchall()
        hours_by_store = load_effective_hours(conn)

        total = 0
        for store_id, meter_id, data_resolution in stores:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ts, received_active_power_kwh, is_synthetic FROM meter_timeseries "
                    "WHERE meter_id = %s ORDER BY ts", (meter_id,),
                )
                full_ts = pd.DataFrame(cur.fetchall(), columns=["ts", "recv_kWh", "is_synthetic"])
            if full_ts.empty:
                continue

            real_ts = real_ts_all[real_ts_all["meter_id"] == meter_id]
            night_baseline = compute_night_baseline(real_ts)
            day_hours = hours_by_store.get(store_id, {})

            status_df = compute_status_for_store(full_ts, day_hours, night_baseline, data_resolution)
            # pandas는 object 컬럼에 들어간 Python None을 저장 시 조용히 NaN(float)으로
            # 바꿔버리는 경우가 있다(congestion_level이 '영업중'이 아닐 때 NULL이어야 하는데
            # np.where(..., None)으로 넣은 None이 DataFrame에 들어가면서 NaN이 되는 걸 실측으로
            # 확인함) - CHECK 제약(congestion_level IN ('상','중','하'))을 위반하므로 DB 적재
            # 직전에 명시적으로 다시 None으로 되돌린다.
            status_df = status_df.replace({np.nan: None})

            rows = [
                (store_id, r.ts, r.schedule_status, r.power_status, r.final_status, r.congestion_level)
                for r in status_df.itertuples(index=False)
            ]
            n = bulk_insert(
                conn, "store_operating_status",
                ["store_id", "ts", "schedule_status", "power_status", "final_status", "congestion_level"],
                rows,
                on_conflict="(store_id, ts) DO UPDATE SET "
                             "schedule_status=EXCLUDED.schedule_status, power_status=EXCLUDED.power_status, "
                             "final_status=EXCLUDED.final_status, congestion_level=EXCLUDED.congestion_level, "
                             "computed_at=now()",
                page_size=20_000,
            )
            total += n
            print(f"  store_id={store_id} meter_id={meter_id}: {n}행 (night_baseline={night_baseline:.3f}kWh)")

    print(f"\nstore_operating_status 적재 총 {total}행")


if __name__ == "__main__":
    main()
