# -*- coding: utf-8 -*-
"""
2-2단계: sql/schema.sql을 실행해 7개 테이블을 생성한다.

--reset을 주면 기존 테이블을 CASCADE로 먼저 지우고 새로 만든다(개발 중 스키마를
바꿔가며 반복 실행하기 위함). 기본(옵션 없음)은 CREATE TABLE IF NOT EXISTS라서
이미 스키마가 있어도 안전하게 재실행할 수 있다.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ami_db.db import get_raw_connection  # noqa: E402

SCHEMA_SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "schema.sql"

# FK 의존성 역순(참조하는 테이블 -> 참조받는 테이블 순서로 지워야 하지만,
# CASCADE를 쓰면 순서 상관없이 한 번에 지울 수 있다. 그래도 가독성을 위해
# 의존성 역순으로 나열한다: meters를 참조하는 테이블들을 먼저, meters를 마지막에).
TABLES_DROP_ORDER = [
    "google_places_cache",
    "anomaly_events",
    "store_operating_status",
    "store_operating_hours",
    "stores",
    "meter_timeseries",
    "meters",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="기존 테이블을 CASCADE로 지우고 재생성")
    args = parser.parse_args()

    schema_sql = SCHEMA_SQL_PATH.read_text(encoding="utf-8")

    with get_raw_connection() as conn:
        with conn.cursor() as cur:
            if args.reset:
                print("--reset: 기존 테이블 삭제 중...")
                for table in TABLES_DROP_ORDER:
                    cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE;")
                    print(f"  DROP TABLE {table}")
            print("schema.sql 실행 중...")
            cur.execute(schema_sql)

    print("완료: 7개 테이블 생성됨.")


if __name__ == "__main__":
    main()
