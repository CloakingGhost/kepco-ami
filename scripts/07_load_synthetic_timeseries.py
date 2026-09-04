# -*- coding: utf-8 -*-
"""
2-5단계 후반: 06단계가 저장한 parquet을 읽어 meter_timeseries(is_synthetic=true)에 적재.

DO NOTHING이 아니라 DO UPDATE를 쓰는 이유: 06을 재실행해 같은 (meter_id, ts)에 대해
새 합성값(예: 데모 시나리오 수정 후 재주입)을 만들어도, 이 로더가 DO NOTHING이면
이미 존재하는 행은 조용히 무시되어 새 값이 절대 반영되지 않는 버그가 있었다(실측으로
확인 - 06을 아무리 다시 돌려도 이미 로드된 날짜의 값은 예전 그대로였음). 02_load_meta_
and_timeseries.py의 기존 upsert 패턴과 동일한 컬럼 조합으로 맞춘다.
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
                on_conflict="(meter_id, ts) DO UPDATE SET "
                             "received_active_power_kwh=EXCLUDED.received_active_power_kwh, "
                             "is_redistributed=EXCLUDED.is_redistributed",
            )
    print(f"meter_timeseries(합성) 적재: {total}행")


if __name__ == "__main__":
    main()
