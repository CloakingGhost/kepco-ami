# -*- coding: utf-8 -*-
"""
2-5단계 후반: 06단계가 저장한 parquet을 읽어 meter_timeseries(is_synthetic=true)에 적재.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ami_db.config import GENERATED_DIR, settings  # noqa: E402
from ami_db.db import bulk_insert, get_raw_connection  # noqa: E402


def main() -> None:
    parquet_path = GENERATED_DIR / f"synthetic_timeseries_{settings.target_dong}.parquet"
    df = pd.read_parquet(parquet_path)
    df = df.replace({np.nan: None})

    cols = ["meter_id", "ts", "received_active_power_kwh", "is_synthetic", "is_redistributed"]
    rows = [
        (r.meter_id, r.ts, r.recv_kWh, bool(r.is_synthetic), bool(r.is_redistributed))
        for r in df.itertuples(index=False)
    ]

    with get_raw_connection() as conn:
        total = 0
        chunk = 20_000
        for i in range(0, len(rows), chunk):
            total += bulk_insert(
                conn, "meter_timeseries", cols, rows[i:i + chunk],
                on_conflict="(meter_id, ts) DO NOTHING",
            )
    print(f"meter_timeseries(합성) 적재: {total}행")


if __name__ == "__main__":
    main()
