# -*- coding: utf-8 -*-
"""
PostgreSQL 연결 헬퍼.

SQLAlchemy engine은 스키마 생성(DDL 실행)과 pandas.read_sql 조회에 쓰고,
대량 적재는 psycopg2의 execute_values로 처리한다(pandas.to_sql의 기본
insert보다 수십~수백 배 빠름 - 이번 최대 적재량인 meter_timeseries
약 31만행 기준으로 체감 차이가 크다).
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterable, Sequence

import psycopg2
import psycopg2.extras
from sqlalchemy import Engine, create_engine

from .config import settings


def get_engine() -> Engine:
    return create_engine(settings.db_url, future=True)


@contextmanager
def get_raw_connection():
    """psycopg2 raw connection (execute_values 벌크 적재용)."""
    conn = psycopg2.connect(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def bulk_insert(
    conn,
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence],
    on_conflict: str | None = None,
    page_size: int = 10_000,
) -> int:
    """
    execute_values 기반 벌크 INSERT.

    on_conflict: 예) "(meter_id, ts) DO NOTHING" 또는
                 "(store_id, ts) DO UPDATE SET final_status=EXCLUDED.final_status, ..."
    이렇게 문자열로 받는 이유는 재실행 시 upsert가 필요한 테이블
    (store_operating_status 등)과 순수 append면 되는 테이블(meter_timeseries 등)의
    요구사항이 달라서, 호출부에서 그때그때 정확한 절을 넘기게 하는 게 가장 명확하기 때문이다.
    """
    rows = list(rows)
    if not rows:
        return 0

    col_list = ", ".join(columns)
    sql = f"INSERT INTO {table} ({col_list}) VALUES %s"
    if on_conflict:
        sql += f" ON CONFLICT {on_conflict}"

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, rows, page_size=page_size)
    return len(rows)
