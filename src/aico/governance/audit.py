"""Audit log cho mọi tool call. Tham số được che secret trước khi ghi."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from psycopg.types.json import Jsonb

from aico.core import utcnow
from aico.db import DictPool

_SECRET_MARKERS = ("token", "secret", "password", "passwd", "api_key", "apikey", "authorization")
_MAX_VALUE = 500


def mask_secrets(value: Any, key: str = "") -> Any:
    if any(m in key.lower() for m in _SECRET_MARKERS):
        return "***"
    if isinstance(value, dict):
        return {k: mask_secrets(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_secrets(v) for v in value]
    if isinstance(value, str) and len(value) > _MAX_VALUE:
        return value[:_MAX_VALUE] + f"…(+{len(value) - _MAX_VALUE})"
    return value


@dataclass(frozen=True)
class AuditEntry:
    role: str
    server: str
    tool: str
    decision: str
    args: dict[str, Any]
    ticket_id: str | None = None
    level: str | None = None
    result_summary: str | None = None
    error: str | None = None
    at: datetime = field(default_factory=utcnow)


class AuditLog(Protocol):
    async def record(self, entry: AuditEntry) -> None: ...


class InMemoryAuditLog:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


class PostgresAuditLog:
    def __init__(self, pool: DictPool) -> None:
        self._pool = pool

    async def record(self, entry: AuditEntry) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "insert into audit_log (at, ticket_id, role, server, tool, level, decision, args,"
                " result_summary, error) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    entry.at,
                    entry.ticket_id,
                    entry.role,
                    entry.server,
                    entry.tool,
                    entry.level,
                    entry.decision,
                    Jsonb(entry.args),
                    entry.result_summary,
                    entry.error,
                ),
            )
