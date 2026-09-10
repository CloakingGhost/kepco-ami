# -*- coding: utf-8 -*-
"""
2-2단계: sql/schema.sql을 실행해 8개 테이블을 생성한다.

운영 서버에서 새 테이블만 추가할 때도 이 스크립트를 옵션 없이 그대로 돌리면 된다 -
schema.sql은 CREATE ... IF NOT EXISTS뿐이고 아래 마이그레이션도 전부 멱등이라 기존
데이터는 건드리지 않는다(예: 2026-09-10 anomaly_narrations 추가).

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
    "anomaly_narrations",
    "anomaly_events",
    "store_operating_status",
    "store_operating_hours",
    "stores",
    "meter_timeseries",
    "meters",
]


ANOMALY_LEVEL_CONSTRAINT = "anomaly_events_level_check3"
ANOMALY_LEVEL_VALUES = ("일반", "주의", "위험")


def migrate_anomaly_level_check(cur) -> None:
    """
    anomaly_events.level CHECK을 2단계('주의','위험') -> 3단계('일반','주의','위험')로 갱신.

    CREATE TABLE IF NOT EXISTS는 이미 존재하는 테이블의 CHECK을 바꿔주지 않고,
    Postgres는 CHECK 제약을 제자리에서 수정할 수 없어서 DROP 후 ADD해야 한다.
    제약 이름이 자동 생성(anomaly_events_level_check)이라 이름을 가정하지 않고
    pg_constraint에서 level을 참조하는 CHECK을 찾아 전부 지운 뒤 새로 만든다.
    --reset 없이 기존 데이터를 보존한 채 반복 실행해도 안전하다(멱등).
    """
    cur.execute(
        """
        SELECT con.conname
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        WHERE rel.relname = 'anomaly_events'
          AND con.contype = 'c'
          AND pg_get_constraintdef(con.oid) LIKE '%level%'
        """
    )
    existing = [r[0] for r in cur.fetchall()]
    if existing == [ANOMALY_LEVEL_CONSTRAINT]:
        return  # 이미 3단계로 마이그레이션됨

    for name in existing:
        cur.execute(f'ALTER TABLE anomaly_events DROP CONSTRAINT "{name}"')
        print(f"  DROP CONSTRAINT {name}")

    values = ", ".join(f"'{v}'" for v in ANOMALY_LEVEL_VALUES)
    cur.execute(
        f"ALTER TABLE anomaly_events ADD CONSTRAINT {ANOMALY_LEVEL_CONSTRAINT} "
        f"CHECK (level IN ({values}))"
    )
    print(f"  ADD CONSTRAINT {ANOMALY_LEVEL_CONSTRAINT} (level IN {ANOMALY_LEVEL_VALUES})")


ANOMALY_SLOT_UNIQUE = "anomaly_events_meter_detected_uniq"


def migrate_anomaly_slot_unique(cur) -> None:
    """
    anomaly_events에 UNIQUE(meter_id, detected_at) 추가.

    전체 재계산 배치(10_detect_anomalies.py)는 DELETE 후 INSERT라 중복이 안 생겼지만,
    증분 배치(21_detect_anomalies_incremental.py)는 같은 슬롯을 여러 번 볼 수밖에 없다
    (지속 조건 판정에 최대 3시간 lookback이 필요해 창이 겹친다). 이 제약이 있어야
    ON CONFLICT DO NOTHING으로 "이미 감지된 슬롯은 조용히 건너뛰기"가 성립한다.

    실측 확인: 추가 시점의 기존 데이터에 (meter_id, detected_at) 중복은 0건이었다.
    """
    cur.execute(
        """
        SELECT 1 FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        WHERE rel.relname = 'anomaly_events' AND con.conname = %s
        """,
        (ANOMALY_SLOT_UNIQUE,),
    )
    if cur.fetchone():
        return  # 이미 적용됨

    cur.execute(
        "SELECT count(*) FROM (SELECT meter_id, detected_at FROM anomaly_events "
        "GROUP BY 1, 2 HAVING count(*) > 1) dup"
    )
    dup_count = cur.fetchone()[0]
    if dup_count:
        print(f"  ⚠ 중복 {dup_count}건이 있어 UNIQUE 제약을 건너뜁니다 - 먼저 정리하세요")
        return

    cur.execute(
        f"ALTER TABLE anomaly_events ADD CONSTRAINT {ANOMALY_SLOT_UNIQUE} "
        f"UNIQUE (meter_id, detected_at)"
    )
    print(f"  ADD CONSTRAINT {ANOMALY_SLOT_UNIQUE} UNIQUE (meter_id, detected_at)")


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

            print("마이그레이션: anomaly_events.level 3단계 CHECK...")
            migrate_anomaly_level_check(cur)

            print("마이그레이션: anomaly_events 슬롯 UNIQUE(증분 배치 멱등성)...")
            migrate_anomaly_slot_unique(cur)

    print("완료: 8개 테이블 생성됨.")


if __name__ == "__main__":
    main()
