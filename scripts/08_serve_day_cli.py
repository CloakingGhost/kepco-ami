# -*- coding: utf-8 -*-
"""날짜별 서빙 함수 스모크테스트 CLI."""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ami_db.db import get_engine  # noqa: E402
from ami_db.serving import get_meter_day_series  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--meter-id", required=True)
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    args = parser.parse_args()

    target_date = date.fromisoformat(args.date)
    engine = get_engine()
    result = get_meter_day_series(engine, args.meter_id, target_date)

    print(f"meter_id={result.meter_id} date={result.target_date} rows={len(result.rows)} is_synthetic={result.is_synthetic}")
    for r in result.rows[:4]:
        print(" ", r)
    if len(result.rows) > 4:
        print(f"  ... ({len(result.rows) - 4}행 더)")


if __name__ == "__main__":
    main()
