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
    동적 계산(실제로는 8737 - 91일치 range가 8736 슬롯보다 1개 더 많다). 1시간 적산 계기는
    15분으로 분해한 뒤를 기준으로 재므로 '15min'으로 잡힌다(resolution.load_real_timeseries).
    """
    step1 = pd.read_csv(step1_csv, encoding="utf-8-sig")
    step1 = step1[step1["선로명"] == settings.target_line].copy()

    ts_a = resolution.load_real_timeseries(timeseries_pkl, set(step1["meter_id"]))
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
    # 재실행하면 해상도/완전성만 원천 재계산값으로 갱신한다(나머지 메타는 최초 적재값 유지).
    return bulk_insert(
        conn, "meters", cols, rows,
        on_conflict="(meter_id) DO UPDATE SET data_completeness=EXCLUDED.data_completeness, "
                    "data_resolution=EXCLUDED.data_resolution",
    )


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


def load_real_timeseries(conn, timeseries_pkl: Path, meter_ids: set) -> int:
    """
    실측 timeseries_clean.pkl에서 matched 계기(~21개)만 필터해 적재. is_synthetic=False 고정.
    NaN은 psycopg2가 그대로 NULL로 넣지 못하므로 None으로 치환한다.

    1시간 적산 계기는 resolution.load_real_timeseries()가 정각값을 15분 구간 4개로 분해한
    값(is_redistributed=True)을 싣는다. 재실행하면 이 계기들의 실측 행을 지우고 다시 넣는다 -
    예전엔 재분배 행을 먼저 넣고 원본을 DO NOTHING으로 넣어서, 재분배 행이 차지한 자리의
    원본 전압·전류가 조용히 버려졌다(A-L-58의 15/30/45분 V/I 6,505개 유실).
    """
    ts = resolution.load_real_timeseries(timeseries_pkl, meter_ids)

    cols_map = [
        ("meter_id", "meter_id"),
        ("time", "ts"),
        ("recv_kWh", "received_active_power_kwh"),
        ("gen_kWh", "generated_active_power_kwh"),
        ("V_A", "voltage_a"), ("V_B", "voltage_b"), ("V_C", "voltage_c"),
        ("I_a", "current_a"), ("I_b", "current_b"), ("I_c", "current_c"),
    ]
    src_cols = [c[0] for c in cols_map] + ["is_redistributed"]
    dst_cols = [c[1] for c in cols_map] + ["is_redistributed", "is_synthetic"]

    subset = ts[src_cols].replace({np.nan: None})
    # numpy.bool_은 psycopg2가 적응하지 못하므로 파이썬 bool로 바꿔 넣는다.
    rows = [tuple(row[:-1]) + (bool(row[-1]), False) for row in subset.itertuples(index=False, name=None)]

    with conn.cursor() as cur:
        cur.execute("DELETE FROM meter_timeseries WHERE meter_id = ANY(%s) AND NOT is_synthetic", (list(meter_ids),))

    total = 0
    chunk = 20_000
    for i in range(0, len(rows), chunk):
        total += bulk_insert(conn, "meter_timeseries", dst_cols, rows[i:i + chunk])
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

        n = load_real_timeseries(conn, timeseries_pkl, matched_meter_ids)
        print(f"[4/4] meter_timeseries(실측) 적재: {n}행")


if __name__ == "__main__":
    main()
