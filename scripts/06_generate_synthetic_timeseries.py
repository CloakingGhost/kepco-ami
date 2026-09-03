# -*- coding: utf-8 -*-
"""
2-5단계 전반: 실측 종료 시점 이후 ~ 오늘까지 합성 시계열을 생성한다(파일 저장만,
DB 쓰기 없음 - 07_load_synthetic_timeseries.py가 이 parquet을 읽어 적재한다).

반드시 04/05(Google Places 운영시간 적재) 이후에 실행해야 한다 - 휴업 시나리오를
심으려면 "그 상가가 언제 영업 중인지"(store_operating_hours)를 알아야 하기 때문.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ami_db import resolution  # noqa: E402
from ami_db.config import GENERATED_DIR, VIZ_OUTPUT, settings  # noqa: E402
from ami_db.db import get_raw_connection  # noqa: E402
from ami_db.status import load_effective_hours  # noqa: E402
from ami_db.synthetic import (  # noqa: E402
    build_slot_profile,
    compute_night_baseline,
    inject_closure_scenario,
    inject_mock_anomaly,
    select_scenarios,
    synthesize_day,
)


def main() -> None:
    matched_csv = GENERATED_DIR / f"store_ami_matched_{settings.target_dong}.csv"
    matched = pd.read_csv(matched_csv, encoding="utf-8-sig")
    meter_ids = matched["meter_id"].tolist()

    print(f"[1/5] 실측 시계열 로드 및 계기별 필터: {len(meter_ids)}개 계기")
    ts_all = pd.read_pickle(VIZ_OUTPUT / "timeseries_clean.pkl")
    ts_all = ts_all[ts_all["meter_id"].isin(meter_ids)]

    with get_raw_connection() as conn:
        # meter_id -> store_id, store_id별 요일별 유효 운영시간(google_places 우선)
        with conn.cursor() as cur:
            cur.execute("SELECT meter_id, store_id FROM stores")
            meter_to_store = dict(cur.fetchall())
        hours_by_store = load_effective_hours(conn)

        # 02단계가 이미 계산해 둔 data_resolution을 재사용 - 여기서도 02와 동일한 코호트
        # 재분배를 각자 다시 계산해서 적용한다(같은 resolution.py 함수 공유, pkl을 독립적으로
        # 재읽는 기존 09단계 패턴과 동일).
        with conn.cursor() as cur:
            cur.execute("SELECT meter_id, data_resolution FROM meters WHERE meter_id = ANY(%s)", (meter_ids,))
            resolution_by_meter = dict(cur.fetchall())

    ratios_by_meter: dict[str, dict[int, tuple]] = {}
    meter_meta_by_meter: dict[str, dict] = {}  # 감사 리포트용 - biz_mid/biz_large/cohort_ids
    skipped_report: dict[str, str] = {}
    for meter_id in meter_ids:
        if resolution_by_meter.get(meter_id) != "1hour":
            continue
        target_row = matched.loc[matched["meter_id"] == meter_id]
        if target_row.empty:
            skipped_report[meter_id] = "matched 목록에 없음 (00단계 산출물에서 누락)"
            continue
        biz_mid = target_row["biz_category_mid"].iloc[0]
        biz_large = target_row["biz_category_large"].iloc[0]
        cohort_ids = resolution.cohort_meter_ids(matched, resolution_by_meter, meter_id, biz_mid, biz_large)
        if len(cohort_ids) < resolution.MIN_COHORT_SIZE:
            skipped_report[meter_id] = f"코호트 부족 (biz_mid={biz_mid}, biz_large={biz_large}, 확보 {len(cohort_ids)}개 < {resolution.MIN_COHORT_SIZE})"
            continue
        cohort_ts = ts_all.loc[ts_all["meter_id"].isin(cohort_ids), ["time", "recv_kWh"]]
        ratios = resolution.build_hourly_ratio_table(cohort_ts)
        if not ratios:
            skipped_report[meter_id] = f"코호트({len(cohort_ids)}개)는 확보했지만 유효한 시간대별 비율이 하나도 없음"
            continue
        ratios_by_meter[meter_id] = ratios
        meter_meta_by_meter[meter_id] = {
            "biz_category_mid": biz_mid, "biz_category_large": biz_large, "cohort_meter_ids": cohort_ids,
        }
        print(f"  -> {meter_id} 합성 프로파일도 코호트 재분배 적용 (코호트 {len(cohort_ids)}개, biz_mid={biz_mid})")

    effective_hours_by_meter = {
        m: hours_by_store.get(meter_to_store[m], {}) for m in meter_ids if m in meter_to_store
    }

    # 전 계기 공통으로 쓸 합성 날짜 범위: (모든 매칭 계기 중 실측이 가장 늦게 끝난 날짜) + 1일 ~ 오늘.
    # 가장 늦은 날짜를 기준으로 잡아야 어떤 계기도 실측 구간과 합성 구간이 겹치지 않는다.
    last_real_date = ts_all.groupby("meter_id")["time"].max().dt.date.max()
    today = date.today()
    synthetic_dates = [last_real_date + timedelta(days=i) for i in range(1, (today - last_real_date).days + 1)]
    print(f"[2/5] 합성 날짜 범위: {synthetic_dates[0] if synthetic_dates else '없음'} ~ {today} ({len(synthetic_dates)}일)")

    rng = np.random.default_rng(settings.rng_seed)
    scenarios = select_scenarios(meter_ids, synthetic_dates, effective_hours_by_meter, rng)
    scenario_by_key = {(s.meter_id, s.target_date): s for s in scenarios}
    print(f"[3/5] 시나리오 선정: 휴업 {sum(1 for s in scenarios if s.kind=='closure')}건, "
          f"이상치 {sum(1 for s in scenarios if s.kind=='anomaly')}건")

    print("[4/5] 계기별 프로파일 산출 및 일별 합성 생성...")
    all_days = []
    scenario_log_rows = []
    for meter_id in meter_ids:
        meter_ts = ts_all[ts_all["meter_id"] == meter_id]
        if meter_ts.empty:
            print(f"  ⚠ {meter_id}: 실측 시계열이 없어 건너뜀")
            continue

        ratios = ratios_by_meter.get(meter_id)
        is_redistributed_meter = ratios is not None
        if is_redistributed_meter:
            # 프로파일 산출 전에 정각 실측을 코호트 비율로 재분배해 15/30/45분을 보충한다 -
            # 이렇게 안 하면 이 계기들의 (요일x슬롯) 그룹이 91일 내내 전부 NaN이라
            # build_slot_profile이 여전히 NaN을 그대로 반환하고 만다(정각만 프로파일링됨).
            meter_ts = resolution.redistribute_hourly_store(meter_ts[["time", "recv_kWh"]], ratios)

        profile = build_slot_profile(meter_ts)
        night_baseline = compute_night_baseline(meter_ts)

        for d in synthetic_dates:
            day_df = synthesize_day(profile, d, rng)
            scenario = scenario_by_key.get((meter_id, d))
            if scenario is not None:
                if scenario.kind == "closure":
                    day_df = inject_closure_scenario(day_df, night_baseline, scenario.slot, rng)
                elif scenario.kind == "anomaly":
                    day_df = inject_mock_anomaly(day_df, scenario.slot, multiplier=6.0, rng=rng)
                scenario_log_rows.append({
                    "meter_id": meter_id, "date": d, "kind": scenario.kind,
                    "slot": scenario.slot, "detail": scenario.detail,
                })
            day_df["meter_id"] = meter_id
            # 재분배 적용 계기(A-L-58)는 15/30/45분 슬롯(정각이 아닌 슬롯)이 코호트 비율로
            # 추정된 프로파일에서 파생됐음을 그대로 물려준다 - 정각(slot%4==0) 슬롯은 실측
            # 정각 분포에서 샘플링된 것이라 False 유지.
            day_df["is_redistributed"] = (day_df["slot"] % 4 != 0) if is_redistributed_meter else False
            all_days.append(day_df)

    synthetic_ts = pd.concat(all_days, ignore_index=True)
    synthetic_ts = synthetic_ts[["meter_id", "ts", "recv_kWh", "is_redistributed"]]
    synthetic_ts["is_synthetic"] = True

    # 코호트 비율표는 DB/parquet 어디에도 남지 않으므로(각 실행마다 재계산 후 소비하고 버림),
    # "이 합성 데이터가 실제로 어떤 비율로 재분배됐는지" 감사용 JSON을 별도로 남긴다.
    redistributed_counts = (
        synthetic_ts[synthetic_ts["is_redistributed"]].groupby("meter_id").size().to_dict()
    )
    applied_report = {
        meter_id: {
            **meter_meta_by_meter[meter_id],
            "hourly_ratios": {str(h): list(r) for h, r in ratios_by_meter[meter_id].items()},
            "redistributed_row_count": int(redistributed_counts.get(meter_id, 0)),
        }
        for meter_id in ratios_by_meter
    }
    report_path = GENERATED_DIR / f"redistribution_ratios_synthetic_{settings.target_dong}.json"
    resolution.write_redistribution_report(
        report_path, "06_generate_synthetic_timeseries.py", applied_report, skipped_report
    )

    print(f"[5/5] 저장 중... 총 {len(synthetic_ts)}행")
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    parquet_path = GENERATED_DIR / f"synthetic_timeseries_{settings.target_dong}.parquet"
    log_path = GENERATED_DIR / f"synthetic_scenario_log_{settings.target_dong}.csv"
    synthetic_ts.to_parquet(parquet_path, index=False)
    pd.DataFrame(scenario_log_rows).to_csv(log_path, index=False, encoding="utf-8-sig")

    print(f"저장: {parquet_path}")
    print(f"저장: {report_path} (코호트 비율표 감사 리포트)")
    print(f"저장: {log_path} ({len(scenario_log_rows)}건 시나리오 기록 - QA용, DB 비적재)")


if __name__ == "__main__":
    main()
