from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from psycopg import AsyncConnection

from aico.db import create_pool, migrate
from aico.registry import Company, load_company
from aico.tickets import InMemoryTicketStore
from aico.tickets.postgres import PostgresTicketStore

ROOT = Path(__file__).resolve().parents[1]
COMPANY_DIR = ROOT / "company"

_TABLES = "audit_log, budget_reservations, approvals, ticket_events, tickets"


def pytest_asyncio_loop_factories(config: object, item: object) -> dict[str, object]:
    # psycopg async không chạy được trên ProactorEventLoop mặc định của Windows.
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}


@pytest.fixture
def company() -> Company:
    return load_company(COMPANY_DIR)


@pytest.fixture
def company_copy(tmp_path: Path) -> Path:
    """Bản sao company/ để test sửa cấu hình."""
    target = tmp_path / "company"
    shutil.copytree(COMPANY_DIR, target)
    return target


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    url = os.environ.get("AICO_TEST_DATABASE_URL")
    if url:
        yield url
        return
    pgserver = pytest.importorskip("pgserver", reason="cần pgserver hoặc AICO_TEST_DATABASE_URL")
    data_dir = Path(tempfile.mkdtemp(prefix="aico-pg-"))
    server = pgserver.get_server(data_dir, cleanup_mode="delete")
    yield server.get_uri()
    server.cleanup()


async def reset_database(url: str) -> None:
    await migrate(url)
    async with await AsyncConnection.connect(url, autocommit=True) as conn:
        await conn.execute(f"truncate {_TABLES} restart identity cascade")


@pytest.fixture
async def pg_clean(pg_url: str) -> AsyncIterator[str]:
    await reset_database(pg_url)
    yield pg_url


@pytest.fixture(params=["memory", "postgres"])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[object]:
    if request.param == "memory":
        yield InMemoryTicketStore()
        return
    url = request.getfixturevalue("pg_url")
    await reset_database(url)
    pool = create_pool(url)
    await pool.open()
    try:
        yield PostgresTicketStore(pool)
    finally:
        await pool.close()
