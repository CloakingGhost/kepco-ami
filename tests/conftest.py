# -*- coding: utf-8 -*-
"""
db/tests/ 공통 픽스처.

이 프로젝트의 서빙 로직(ami_db.serving)은 00~10 배치 스크립트가 실제 Postgres에
미리 계산해 적재해 둔 결과를 그대로 읽기만 하는 얇은 레이어다. mock DB로 대체하면
실제 조인/existence-check 버그를 놓치므로, 이 테스트 스위트는 항상 실제 Postgres
(db/docker-compose.yml, contest_db)를 대상으로 하는 통합 테스트다.

로컬에 DB가 안 떠 있으면 에러로 죽는 대신 전체 스위트를 skip 처리한다.
"""
import sys
from pathlib import Path

DB_ROOT = Path(__file__).resolve().parent.parent
if str(DB_ROOT) not in sys.path:
    sys.path.insert(0, str(DB_ROOT))

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from ami_db.db import get_engine  # noqa: E402


def _db_available() -> bool:
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


DB_AVAILABLE = _db_available()


@pytest.fixture(scope="session", autouse=True)
def _skip_if_no_db():
    if not DB_AVAILABLE:
        pytest.skip(
            "Postgres(contest_db)에 접속할 수 없습니다 - "
            "places-api-project-postgres-1 컨테이너가 떠 있는지 확인하세요."
        )


@pytest.fixture(scope="session")
def engine():
    return get_engine()


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from app.serving_api import app

    with TestClient(app) as c:
        yield c
