"""Ticket store trên Postgres. Lease dùng SELECT ... FOR UPDATE SKIP LOCKED."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import AsyncConnection, sql
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from aico.core import (
    TERMINAL_STATUSES,
    WAITING_STATUSES,
    Artifact,
    Event,
    Ticket,
    TicketStatus,
)
from aico.db import DictPool
from aico.tickets.state_machine import check_transition
from aico.tickets.store import UPDATABLE_FIELDS, TicketExists, TicketNotFound, _jsonable

_COLUMNS = (
    "id, title, body, source, source_ref, project, labels, department, owner_role, status, risk,"
    " budget_usd, spent_usd, spent_tokens, parent_id, artifacts, trace_id, lease_owner,"
    " lease_until, pending_resume, approval_deadline, created_at, updated_at"
)
_JSON_FIELDS = {"labels", "artifacts", "pending_resume"}
_TERMINAL = [s.value for s in TERMINAL_STATUSES]
_WAITING = [s.value for s in WAITING_STATUSES]


def _row_to_ticket(row: DictRow) -> Ticket:
    return Ticket.model_validate(row)


def _ticket_params(ticket: Ticket) -> dict[str, Any]:
    data = ticket.model_dump(mode="python")
    data["artifacts"] = [a.model_dump() for a in ticket.artifacts]
    for key in _JSON_FIELDS:
        data[key] = Jsonb(data[key]) if data[key] is not None else None
    for key in ("source", "status", "risk"):
        value = data[key]
        data[key] = value.value if value is not None else None
    return data


class PostgresTicketStore:
    def __init__(self, pool: DictPool) -> None:
        self._pool = pool

    async def _fetch(self, conn: AsyncConnection[DictRow], ticket_id: str, *, lock: bool) -> Ticket:
        sql = f"select {_COLUMNS} from tickets where id = %s" + (" for update" if lock else "")
        cur = await conn.execute(sql, (ticket_id,))
        row = await cur.fetchone()
        if row is None:
            raise TicketNotFound(ticket_id)
        return _row_to_ticket(row)

    async def _event(self, conn: AsyncConnection[DictRow], event: Event) -> None:
        await conn.execute(
            "insert into ticket_events (ticket_id, kind, from_status, to_status, actor, data, at)"
            " values (%s, %s, %s, %s, %s, %s, %s)",
            (
                event.ticket_id,
                event.kind,
                event.from_status.value if event.from_status else None,
                event.to_status.value if event.to_status else None,
                event.actor,
                Jsonb(event.data),
                event.at,
            ),
        )

    async def _insert(self, conn: AsyncConnection[DictRow], ticket: Ticket, actor: str) -> Ticket:
        params = _ticket_params(ticket)
        names = _COLUMNS.replace(" ", "").split(",")
        placeholders = ", ".join(f"%({n})s" for n in names)
        cur = await conn.execute(
            f"insert into tickets ({_COLUMNS}) values ({placeholders})"
            f" on conflict do nothing returning {_COLUMNS}",
            params,
        )
        row = await cur.fetchone()
        if row is None:
            raise TicketExists(ticket.id)
        await self._event(
            conn, Event(ticket_id=ticket.id, kind="created", to_status=ticket.status, actor=actor)
        )
        return _row_to_ticket(row)

    async def create(self, ticket: Ticket, *, actor: str) -> Ticket:
        async with self._pool.connection() as conn, conn.transaction():
            return await self._insert(conn, ticket, actor)

    async def create_from_source(self, ticket: Ticket, *, actor: str) -> tuple[Ticket, bool]:
        async with self._pool.connection() as conn, conn.transaction():
            if ticket.source_ref is not None:
                cur = await conn.execute(
                    f"select {_COLUMNS} from tickets where source_ref = %s", (ticket.source_ref,)
                )
                row = await cur.fetchone()
                if row is not None:
                    return _row_to_ticket(row), False
            try:
                return await self._insert(conn, ticket, actor), True
            except TicketExists:
                if ticket.source_ref is None:
                    raise
        # Một tiến trình khác vừa tạo cùng source_ref: đọc lại bản đó.
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"select {_COLUMNS} from tickets where source_ref = %s", (ticket.source_ref,)
            )
            row = await cur.fetchone()
            if row is None:
                raise TicketExists(ticket.id)
            return _row_to_ticket(row), False

    async def get(self, ticket_id: str) -> Ticket:
        async with self._pool.connection() as conn:
            return await self._fetch(conn, ticket_id, lock=False)

    async def list(self, *, statuses: set[TicketStatus] | None = None) -> list[Ticket]:
        async with self._pool.connection() as conn:
            if statuses is None:
                cur = await conn.execute(f"select {_COLUMNS} from tickets order by created_at")
            else:
                cur = await conn.execute(
                    f"select {_COLUMNS} from tickets where status = any(%s) order by created_at",
                    ([s.value for s in statuses],),
                )
            return [_row_to_ticket(r) for r in await cur.fetchall()]

    async def transition(
        self,
        ticket_id: str,
        to: TicketStatus,
        *,
        actor: str,
        data: dict[str, Any] | None = None,
    ) -> Ticket:
        async with self._pool.connection() as conn, conn.transaction():
            ticket = await self._fetch(conn, ticket_id, lock=True)
            if ticket.status == to:
                return ticket
            check_transition(ticket.status, to)
            cur = await conn.execute(
                f"update tickets set status = %s, updated_at = now() where id = %s"
                f" returning {_COLUMNS}",
                (to.value, ticket_id),
            )
            row = await cur.fetchone()
            assert row is not None
            await self._event(
                conn,
                Event(
                    ticket_id=ticket_id,
                    kind="status",
                    from_status=ticket.status,
                    to_status=to,
                    actor=actor,
                    data=data or {},
                ),
            )
            return _row_to_ticket(row)

    async def update(self, ticket_id: str, *, actor: str, **fields: Any) -> Ticket:
        unknown = set(fields) - UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"không cập nhật được trường: {sorted(unknown)}")
        params: dict[str, Any] = {}
        for key, value in fields.items():
            if key in _JSON_FIELDS:
                params[key] = Jsonb(value)
            elif hasattr(value, "value"):
                params[key] = value.value
            else:
                params[key] = value
        # Tên cột chỉ lấy từ UPDATABLE_FIELDS, ghép bằng psycopg.sql để không nối chuỗi SQL.
        assignments = sql.SQL(", ").join(
            sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder(k)) for k in params
        )
        params["_id"] = ticket_id
        query = sql.SQL(
            "update tickets set {}, updated_at = now() where id = %(_id)s returning " + _COLUMNS
        ).format(assignments)
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(query, params)
            row = await cur.fetchone()
            if row is None:
                raise TicketNotFound(ticket_id)
            await self._event(
                conn,
                Event(ticket_id=ticket_id, kind="updated", actor=actor, data=_jsonable(fields)),
            )
            return _row_to_ticket(row)

    async def add_artifact(self, ticket_id: str, artifact: Artifact, *, actor: str) -> Ticket:
        async with self._pool.connection() as conn, conn.transaction():
            ticket = await self._fetch(conn, ticket_id, lock=True)
            if artifact in ticket.artifacts:
                return ticket
            artifacts = [a.model_dump() for a in [*ticket.artifacts, artifact]]
            cur = await conn.execute(
                f"update tickets set artifacts = %s, updated_at = now() where id = %s"
                f" returning {_COLUMNS}",
                (Jsonb(artifacts), ticket_id),
            )
            row = await cur.fetchone()
            assert row is not None
            await self._event(
                conn,
                Event(
                    ticket_id=ticket_id, kind="artifact", actor=actor, data=artifact.model_dump()
                ),
            )
            return _row_to_ticket(row)

    async def set_pending_resume(
        self, ticket_id: str, payload: dict[str, Any], *, actor: str
    ) -> Ticket:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                f"update tickets set pending_resume = %s, updated_at = now() where id = %s"
                f" returning {_COLUMNS}",
                (Jsonb(payload), ticket_id),
            )
            row = await cur.fetchone()
            if row is None:
                raise TicketNotFound(ticket_id)
            await self._event(
                conn,
                Event(ticket_id=ticket_id, kind="resume_requested", actor=actor, data=payload),
            )
            return _row_to_ticket(row)

    async def take_pending_resume(self, ticket_id: str, *, owner: str) -> dict[str, Any] | None:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "update tickets t set pending_resume = null"
                " from (select id, pending_resume from tickets where id = %s for update) old"
                " where t.id = old.id and t.lease_owner = %s and old.pending_resume is not null"
                " returning old.pending_resume",
                (ticket_id, owner),
            )
            row = await cur.fetchone()
            return None if row is None else row["pending_resume"]

    async def claim(self, *, owner: str, lease_seconds: float) -> Ticket | None:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "select id from tickets"
                " where status <> all(%(terminal)s)"
                "   and (lease_until is null or lease_until <= now())"
                "   and (status <> all(%(waiting)s) or pending_resume is not null)"
                " order by updated_at"
                " limit 1 for update skip locked",
                {"terminal": _TERMINAL, "waiting": _WAITING},
            )
            row = await cur.fetchone()
            if row is None:
                return None
            cur = await conn.execute(
                f"update tickets set lease_owner = %s,"
                f" lease_until = now() + make_interval(secs => %s), updated_at = now()"
                f" where id = %s returning {_COLUMNS}",
                (owner, lease_seconds, row["id"]),
            )
            claimed = await cur.fetchone()
            assert claimed is not None
            return _row_to_ticket(claimed)

    async def renew(self, ticket_id: str, *, owner: str, lease_seconds: float) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "update tickets set lease_until = now() + make_interval(secs => %s)"
                " where id = %s and lease_owner = %s",
                (lease_seconds, ticket_id, owner),
            )
            return cur.rowcount == 1

    async def release(self, ticket_id: str, *, owner: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "update tickets set lease_owner = null, lease_until = null"
                " where id = %s and lease_owner = %s",
                (ticket_id, owner),
            )

    async def events(self, ticket_id: str) -> list[Event]:
        async with self._pool.connection() as conn:
            await self._fetch(conn, ticket_id, lock=False)
            cur = await conn.execute(
                "select ticket_id, kind, from_status, to_status, actor, data, at"
                " from ticket_events where ticket_id = %s order by id",
                (ticket_id,),
            )
            return [Event.model_validate(r) for r in await cur.fetchall()]

    async def overdue_approvals(self, now: datetime) -> list[Ticket]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"select {_COLUMNS} from tickets"
                " where status = any(%s) and pending_resume is null"
                " and approval_deadline is not null and approval_deadline <= %s",
                (
                    [
                        TicketStatus.AWAITING_PLAN_APPROVAL.value,
                        TicketStatus.AWAITING_MERGE_APPROVAL.value,
                    ],
                    now,
                ),
            )
            return [_row_to_ticket(r) for r in await cur.fetchall()]
