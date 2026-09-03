# -*- coding: utf-8 -*-
"""
2-2단계: meters / stores / store_operating_hours(source='ksic_estimate') / 실측 meter_timeseries 적재.

입력(읽기 전용):
  visualize_analyis_data/output/step1_meta_classified.csv  (A선로 필터해서 meters 전체 64개)
  visualize_analyis_data/output/timeseries_clean.pkl        (실측 15분 시계열, matched 계기만 필터)
  db/output/generated/store_ami_matched_화곡동.csv          (00단계 산출물)
  db/output/generated/store_ami_excluded_화곡동.csv         (00단계 산출물, match_status 파생용)

meter_summary.csv의 completeness 컬럼은 더 이상 쓰지 않는다 - 8736(91일x96) 고정 분모를
전제하고 있어 실제 range(8737)와 어긋나고, 계기별 해상도(data_resolution) 차이도 반영하지
못한다. 대신 이 스크립트가 timeseries_clean.pkl에서 직접 completeness/resolution을 재계산한다.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ami_db import resolution  # noqa: E402
from ami_db.config import GENERATED_DIR, VIZ_OUTPUT, settings  # noqa: E402
from ami_db.db import bulk_insert, get_raw_connection  # noqa: E402
from ami_db.hours_parser import parse_estimate_hours_range  # noqa: E402


def load_meters(conn, step1_csv: Path, timeseries_pkl: Path, matched_meter_ids: set, excluded_meter_ids: set) -> int:
    """
    meters 테이블 적재. 스코프는 A선로 전체(match_status와 무관하게 다 넣는다 -
    "매칭 안 된 계기가 왜 안 됐는지"까지 테이블 자체에서 드러나게 하려는 목적,
    계획서 아키텍처 결정 참고).

    data_completeness/data_resolution은 timeseries_clean.pkl에서 A선로 64개 전체에 대해
    직접 재계산한다. 그리드 크기는 8736 고정이 아니라 실측 min~max 타임스탬프 range로
    동적 계산(실제로는 8737 - 91일치 range가 8736 슬롯보다 1개 더 많다).
    """
    step1 = pd.read_csv(step1_csv, encoding="utf-8-sig")
    step1 = step1[step1["선로명"] == settings.target_line].copy()

    ts_all = pd.read_pickle(timeseries_pkl)
    ts_a = ts_all[ts_all["meter_id"].isin(set(step1["meter_id"]))]
    grid_size = len(pd.date_range(ts_a["time"].min(), ts_a["time"].max(), freq="15min"))

    completeness_by_meter: dict[str, float] = {}
    resolution_by_meter: dict[str, str] = {}
    for meter_id, g in ts_a.groupby("meter_id"):
        completeness_by_meter[meter_id] = float(g["recv_kWh"].notna().sum()) / grid_size * 100 if grid_size else 0.0
        resolution_by_meter[meter_id] = resolution.detect_data_resolution(g)

    def resolve_match_status(row) -> str:
        if row["meter_id"] in matched_meter_ids:
            return "matched"
        if bool(row["매장매칭_적격"]):
            return "eligible_unmatched"
        return "ineligible"

    step1["match_status"] = step1.apply(resolve_match_status, axis=1)

    def supply_type_of(v):
        return v if isinstance(v, str) else None

    rows = []
    for _, r in step1.iterrows():
        meter_id = r["meter_id"]
        rows.append((
            meter_id,
            settings.target_line,
            str(r.get("구간번호")) if pd.notna(r.get("구간번호")) else None,
            supply_type_of(r.get("공급방식")),
            float(r["수전전력"]) if pd.notna(r.get("수전전력")) else None,
            r.get("계약종별") if pd.notna(r.get("계약종별")) else None,
            r.get("사용용도") if pd.notna(r.get("사용용도")) else None,
            float(r["배수"]) if pd.notna(r.get("배수")) else None,
            r.get("전기차/분산형 여부") if pd.notna(r.get("전기차/분산형 여부")) else None,
            r.get("주생산품") if pd.notna(r.get("주생산품")) else None,
            r.get("산업분류") if pd.notna(r.get("산업분류")) else None,
            r.get("카테고리분류") if pd.notna(r.get("카테고리분류")) else None,
            r.get("전력분류") if pd.notna(r.get("전력분류")) else None,
            completeness_by_meter.get(meter_id, 0.0),
            resolution_by_meter.get(meter_id, "15min"),
            r["match_status"],
        ))

    cols = [
        "meter_id", "line_name", "section_no", "supply_type", "contract_power_kw",
        "contract_type", "usage_purpose", "multiplier", "has_der", "main_product",
        "ksic_code", "category_class", "power_class", "data_completeness", "data_resolution", "match_status",
    ]
    return bulk_insert(conn, "meters", cols, rows, on_conflict="(meter_id) DO NOTHING")


def load_stores(conn, matched_csv: Path) -> dict:
    """stores 적재 + {meter_id: store_id} 매핑 반환 (다음 단계들이 필요로 함)."""
    matched = pd.read_csv(matched_csv, encoding="utf-8-sig")
    cols = [
        "meter_id", "name", "branch_name", "road_address", "building_name", "floor_info",
        "longitude", "latitude", "biz_category_large", "biz_category_mid", "ksic_code", "dong_name",
    ]
    rows = []
    for _, r in matched.iterrows():
        rows.append(tuple(
            (None if pd.isna(r[c]) else r[c]) for c in cols
        ))

    with conn.cursor() as cur:
        col_list = ", ".join(cols)
        placeholders = ", ".join(["%s"] * len(cols))
        cur.executemany(
            f"INSERT INTO stores ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT (meter_id) DO NOTHING",
            rows,
        )
        cur.execute("SELECT store_id, meter_id FROM stores")
        return {meter_id: store_id for store_id, meter_id in cur.fetchall()}


def load_ksic_estimated_hours(conn, matched_csv: Path, store_id_map: dict) -> int:
    """
    store_ami_matched_화곡동.csv의 estimated_hours_range를 요일별 7행으로 펼쳐 적재.
    이 값은 04/05단계에서 Google Places 실측이 붙기 전까지 쓰는 잠정치이며,
    source='ksic_estimate'로 명확히 구분되므로 이후 실측이 들어와도 삭제하지 않는다.
    """
    matched = pd.read_csv(matched_csv, encoding="utf-8-sig")
    rows = []
    for _, r in matched.iterrows():
        store_id = store_id_map.get(r["meter_id"])
        if store_id is None:
            continue
        for hr in parse_estimate_hours_range(r["estimated_hours_range"]):
            rows.append((
                store_id, "ksic_estimate", hr.day_of_week,
                hr.open_time, hr.close_time, hr.is_closed, hr.is_24h, None,
            ))
    cols = ["store_id", "source", "day_of_week", "open_time", "close_time", "is_closed", "is_24h", "raw_hours_text"]
    return bulk_insert(conn, "store_operating_hours", cols, rows, on_conflict="(store_id, source, day_of_week) DO NOTHING")


def load_real_timeseries(conn, timeseries_pkl: Path, matched: pd.DataFrame) -> int:
    """
    실측 timeseries_clean.pkl에서 matched 계기(~21개)만 필터해 적재.
    is_synthetic=False 고정 - 이 값이 있어야 서빙 로직이 실측/합성을 구분할 수 있다.
    NaN은 psycopg2가 그대로 NULL로 넣지 못하므로 None으로 치환한다.

    data_resolution='1hour'인 계기 중 같은 업종 코호트(15min 정상 해상도)가 3개 이상
    있으면 resolution.build_hourly_ratio_table()/redistribute_hourly_store()로 15/30/45분을
    보충한다(정각 실측에 코호트 상대 비율을 곱한 추정값, is_redistributed=True). 코호트가
    부족한 나머지 계기는 원래 정각 실측만 그대로 적재하고 넘어간다 - meters.data_resolution
    플래그가 이미 "이 계기는 1시간 해상도"임을 투명하게 드러내므로 그걸로 충분하다.
    """
    meter_ids = set(matched["meter_id"])
    ts = pd.read_pickle(timeseries_pkl)
    ts = ts[ts["meter_id"].isin(meter_ids)].copy()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT meter_id, data_resolution FROM meters WHERE meter_id = ANY(%s)", (list(meter_ids),)
        )
        resolution_by_meter = dict(cur.fetchall())

    hourly_meters = [m for m in meter_ids if resolution_by_meter.get(m) == "1hour"]

    # (meter_id, ts, recv_kWh) - 재분배로 새로 계산된 15/30/45분 값. 원본 행이 이미 있든(대개
    # recv_kWh=NULL이지만 전압/전류는 정상) 아예 없든(47개 완전 누락 케이스) 구분하지 않고
    # 나중에 upsert로 먼저 적용한다.
    extra_rows: list[tuple] = []
    # 아래 두 dict는 DB/parquet 어디에도 남지 않는 "이 코호트 비율표가 실제로 뭐였는지"를
    # 감사할 수 있게 output/generated/에 JSON으로 남기기 위한 것 (resolution.write_redistribution_report 참고).
    applied_report: dict[str, dict] = {}
    skipped_report: dict[str, str] = {}
    for meter_id in hourly_meters:
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

        cohort_ts = ts.loc[ts["meter_id"].isin(cohort_ids), ["time", "recv_kWh"]]
        ratios = resolution.build_hourly_ratio_table(cohort_ts)
        if not ratios:
            skipped_report[meter_id] = f"코호트({len(cohort_ids)}개)는 확보했지만 유효한 시간대별 비율이 하나도 없음"
            continue

        target_ts = ts.loc[ts["meter_id"] == meter_id, ["time", "recv_kWh"]]
        redistributed = resolution.redistribute_hourly_store(target_ts, ratios)
        new_only = redistributed[redistributed["is_redistributed"]]
        for row in new_only.itertuples(index=False):
            extra_rows.append((meter_id, row.time, float(row.recv_kWh)))
        applied_report[meter_id] = {
            "biz_category_mid": biz_mid,
            "biz_category_large": biz_large,
            "cohort_meter_ids": cohort_ids,
            "hourly_ratios": {str(h): list(r) for h, r in ratios.items()},
            "redistributed_row_count": int(len(new_only)),
        }
        print(f"    -> {meter_id} 코호트 재분배: {len(new_only)}행 신규/보충 (코호트 {len(cohort_ids)}개, biz_mid={biz_mid})")

    report_path = GENERATED_DIR / f"redistribution_ratios_{settings.target_dong}.json"
    resolution.write_redistribution_report(report_path, "02_load_meta_and_timeseries.py", applied_report, skipped_report)
    print(f"    (코호트 비율표 감사 리포트 저장: {report_path})")

    cols_map = [
        ("meter_id", "meter_id"),
        ("time", "ts"),
        ("recv_kWh", "received_active_power_kwh"),
        ("gen_kWh", "generated_active_power_kwh"),
        ("V_A", "voltage_a"), ("V_B", "voltage_b"), ("V_C", "voltage_c"),
        ("I_a", "current_a"), ("I_b", "current_b"), ("I_c", "current_c"),
    ]
    src_cols = [c[0] for c in cols_map]
    dst_cols = [c[1] for c in cols_map] + ["is_synthetic", "is_redistributed"]

    subset = ts[src_cols].replace({np.nan: None})
    rows = [tuple(row) + (False, False) for row in subset.itertuples(index=False, name=None)]

    total = 0
    chunk = 20_000

    # 재분배 값을 먼저 upsert한다(recv_kWh/is_redistributed만 갱신 - 이미 있던 행의 전압/전류는
    # 건드리지 않는다). 그 다음에 원본 전체 행을 DO NOTHING으로 적재해야, 재분배로 채운 값이
    # 원본의 NULL로 덮어써지지 않는다(먼저 원본을 넣으면 재분배 값이 DO NOTHING에 막혀 유실됨).
    if extra_rows:
        extra_full_rows = [
            (meter_id, ts_value, recv_kwh, None, None, None, None, None, None, None, False, True)
            for meter_id, ts_value, recv_kwh in extra_rows
        ]
        for i in range(0, len(extra_full_rows), chunk):
            total += bulk_insert(
                conn, "meter_timeseries", dst_cols, extra_full_rows[i:i + chunk],
                on_conflict="(meter_id, ts) DO UPDATE SET "
                             "received_active_power_kwh=EXCLUDED.received_active_power_kwh, "
                             "is_redistributed=EXCLUDED.is_redistributed",
            )

    for i in range(0, len(rows), chunk):
        total += bulk_insert(
            conn, "meter_timeseries", dst_cols, rows[i:i + chunk],
            on_conflict="(meter_id, ts) DO NOTHING",
        )
    return total


def main() -> None:
    step1_csv = VIZ_OUTPUT / "step1_meta_classified.csv"
    timeseries_pkl = VIZ_OUTPUT / "timeseries_clean.pkl"
    matched_csv = GENERATED_DIR / f"store_ami_matched_{settings.target_dong}.csv"

    matched = pd.read_csv(matched_csv, encoding="utf-8-sig")
    matched_meter_ids = set(matched["meter_id"])

    with get_raw_connection() as conn:
        n = load_meters(conn, step1_csv, timeseries_pkl, matched_meter_ids, set())
        print(f"[1/4] meters 적재: {n}건")

        store_id_map = load_stores(conn, matched_csv)
        print(f"[2/4] stores 적재: {len(store_id_map)}건")

        n = load_ksic_estimated_hours(conn, matched_csv, store_id_map)
        print(f"[3/4] store_operating_hours(ksic_estimate) 적재: {n}행 (매장수 x 7요일)")

        n = load_real_timeseries(conn, timeseries_pkl, matched[["meter_id", "biz_category_large", "biz_category_mid"]])
        print(f"[4/4] meter_timeseries(실측) 적재: {n}행")


if __name__ == "__main__":
    main()
