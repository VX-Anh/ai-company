"""Kết nối Postgres và migration bằng file SQL đánh số."""

from __future__ import annotations

from importlib import resources

from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

_MIGRATIONS = "aico.db.migrations"

DictPool = AsyncConnectionPool[AsyncConnection[DictRow]]


def migration_files() -> list[tuple[str, str]]:
    files = sorted(
        (f for f in resources.files(_MIGRATIONS).iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    return [(f.name.removesuffix(".sql"), f.read_text(encoding="utf-8")) for f in files]


async def migrate(conninfo: str) -> list[str]:
    """Áp các migration chưa chạy. Chạy lại nhiều lần không lỗi."""
    applied: list[str] = []
    async with await AsyncConnection.connect(conninfo, autocommit=True) as conn:
        await conn.execute(
            "create table if not exists schema_migrations ("
            " version text primary key, applied_at timestamptz not null default now())"
        )
        # Khóa để hai tiến trình không migrate cùng lúc.
        await conn.execute("select pg_advisory_lock(7310001)")
        try:
            cur = await conn.execute("select version from schema_migrations")
            done = {row[0] for row in await cur.fetchall()}
            for version, sql in migration_files():
                if version in done:
                    continue
                async with conn.transaction():
                    await conn.execute(sql)  # type: ignore[arg-type]
                    await conn.execute(
                        "insert into schema_migrations (version) values (%s)", (version,)
                    )
                applied.append(version)
        finally:
            await conn.execute("select pg_advisory_unlock(7310001)")
    return applied


def create_pool(conninfo: str, *, min_size: int = 1, max_size: int = 10) -> DictPool:
    return AsyncConnectionPool(
        conninfo,
        connection_class=AsyncConnection[DictRow],
        min_size=min_size,
        max_size=max_size,
        kwargs={"row_factory": dict_row},
        open=False,
    )
