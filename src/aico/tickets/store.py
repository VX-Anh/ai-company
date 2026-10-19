"""Giao diện ticket store và bản in-memory dùng cho test và chạy thử."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Protocol

from aico.core import (
    TERMINAL_STATUSES,
    WAITING_STATUSES,
    Artifact,
    Event,
    Ticket,
    TicketStatus,
    utcnow,
)
from aico.tickets.state_machine import check_transition

UPDATABLE_FIELDS = frozenset(
    {"risk", "department", "owner_role", "trace_id", "approval_deadline", "project", "labels"}
)


class TicketNotFound(KeyError):
    pass


class TicketExists(ValueError):
    pass


class TicketStore(Protocol):
    async def create(self, ticket: Ticket, *, actor: str) -> Ticket: ...

    async def create_from_source(self, ticket: Ticket, *, actor: str) -> tuple[Ticket, bool]:
        """Tạo ticket nếu chưa có ticket cùng source_ref. Trả (ticket, đã_tạo_mới)."""
        ...

    async def get(self, ticket_id: str) -> Ticket: ...

    async def list(self, *, statuses: set[TicketStatus] | None = None) -> list[Ticket]: ...

    async def transition(
        self,
        ticket_id: str,
        to: TicketStatus,
        *,
        actor: str,
        data: dict[str, Any] | None = None,
    ) -> Ticket: ...

    async def update(self, ticket_id: str, *, actor: str, **fields: Any) -> Ticket: ...

    async def add_artifact(self, ticket_id: str, artifact: Artifact, *, actor: str) -> Ticket: ...

    async def set_pending_resume(
        self, ticket_id: str, payload: dict[str, Any], *, actor: str
    ) -> Ticket: ...

    async def take_pending_resume(self, ticket_id: str, *, owner: str) -> dict[str, Any] | None: ...

    async def claim(self, *, owner: str, lease_seconds: float) -> Ticket | None: ...

    async def renew(self, ticket_id: str, *, owner: str, lease_seconds: float) -> bool: ...

    async def release(self, ticket_id: str, *, owner: str) -> None: ...

    async def events(self, ticket_id: str) -> list[Event]: ...

    async def overdue_approvals(self, now: datetime) -> list[Ticket]: ...


def is_claimable(ticket: Ticket, now: datetime) -> bool:
    if ticket.status in TERMINAL_STATUSES:
        return False
    if ticket.lease_until is not None and ticket.lease_until > now:
        return False
    return ticket.status not in WAITING_STATUSES or ticket.pending_resume is not None


class InMemoryTicketStore:
    def __init__(self) -> None:
        self._tickets: dict[str, Ticket] = {}
        self._events: dict[str, list[Event]] = {}
        self._lock = asyncio.Lock()

    def _get(self, ticket_id: str) -> Ticket:
        try:
            return self._tickets[ticket_id]
        except KeyError:
            raise TicketNotFound(ticket_id) from None

    def _put(self, ticket: Ticket, event: Event | None = None) -> Ticket:
        ticket = ticket.model_copy(update={"updated_at": utcnow()})
        self._tickets[ticket.id] = ticket
        if event is not None:
            self._events.setdefault(ticket.id, []).append(event)
        return ticket.model_copy(deep=True)

    async def create(self, ticket: Ticket, *, actor: str) -> Ticket:
        async with self._lock:
            if ticket.id in self._tickets:
                raise TicketExists(ticket.id)
            return self._put(
                ticket,
                Event(ticket_id=ticket.id, kind="created", to_status=ticket.status, actor=actor),
            )

    async def create_from_source(self, ticket: Ticket, *, actor: str) -> tuple[Ticket, bool]:
        async with self._lock:
            if ticket.source_ref is not None:
                for existing in self._tickets.values():
                    if existing.source_ref == ticket.source_ref:
                        return existing.model_copy(deep=True), False
            if ticket.id in self._tickets:
                raise TicketExists(ticket.id)
            created = self._put(
                ticket,
                Event(ticket_id=ticket.id, kind="created", to_status=ticket.status, actor=actor),
            )
            return created, True

    async def get(self, ticket_id: str) -> Ticket:
        return self._get(ticket_id).model_copy(deep=True)

    async def list(self, *, statuses: set[TicketStatus] | None = None) -> list[Ticket]:
        items = sorted(self._tickets.values(), key=lambda t: t.created_at)
        return [t.model_copy(deep=True) for t in items if statuses is None or t.status in statuses]

    async def transition(
        self,
        ticket_id: str,
        to: TicketStatus,
        *,
        actor: str,
        data: dict[str, Any] | None = None,
    ) -> Ticket:
        async with self._lock:
            ticket = self._get(ticket_id)
            if ticket.status == to:
                return ticket.model_copy(deep=True)
            check_transition(ticket.status, to)
            event = Event(
                ticket_id=ticket_id,
                kind="status",
                from_status=ticket.status,
                to_status=to,
                actor=actor,
                data=data or {},
            )
            return self._put(ticket.model_copy(update={"status": to}), event)

    async def update(self, ticket_id: str, *, actor: str, **fields: Any) -> Ticket:
        unknown = set(fields) - UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"không cập nhật được trường: {sorted(unknown)}")
        async with self._lock:
            ticket = self._get(ticket_id)
            event = Event(ticket_id=ticket_id, kind="updated", actor=actor, data=_jsonable(fields))
            return self._put(ticket.model_copy(update=fields), event)

    async def add_artifact(self, ticket_id: str, artifact: Artifact, *, actor: str) -> Ticket:
        async with self._lock:
            ticket = self._get(ticket_id)
            if artifact in ticket.artifacts:
                return ticket.model_copy(deep=True)
            event = Event(
                ticket_id=ticket_id, kind="artifact", actor=actor, data=artifact.model_dump()
            )
            return self._put(
                ticket.model_copy(update={"artifacts": [*ticket.artifacts, artifact]}), event
            )

    async def set_pending_resume(
        self, ticket_id: str, payload: dict[str, Any], *, actor: str
    ) -> Ticket:
        async with self._lock:
            ticket = self._get(ticket_id)
            event = Event(ticket_id=ticket_id, kind="resume_requested", actor=actor, data=payload)
            return self._put(ticket.model_copy(update={"pending_resume": payload}), event)

    async def take_pending_resume(self, ticket_id: str, *, owner: str) -> dict[str, Any] | None:
        async with self._lock:
            ticket = self._get(ticket_id)
            if ticket.lease_owner != owner or ticket.pending_resume is None:
                return None
            payload = ticket.pending_resume
            self._put(ticket.model_copy(update={"pending_resume": None}))
            return payload

    async def claim(self, *, owner: str, lease_seconds: float) -> Ticket | None:
        async with self._lock:
            now = utcnow()
            for ticket in sorted(self._tickets.values(), key=lambda t: t.updated_at):
                if is_claimable(ticket, now):
                    claimed = ticket.model_copy(
                        update={
                            "lease_owner": owner,
                            "lease_until": now + timedelta(seconds=lease_seconds),
                        }
                    )
                    return self._put(claimed)
            return None

    async def renew(self, ticket_id: str, *, owner: str, lease_seconds: float) -> bool:
        async with self._lock:
            ticket = self._get(ticket_id)
            if ticket.lease_owner != owner:
                return False
            until = utcnow() + timedelta(seconds=lease_seconds)
            self._put(ticket.model_copy(update={"lease_until": until}))
            return True

    async def release(self, ticket_id: str, *, owner: str) -> None:
        async with self._lock:
            ticket = self._get(ticket_id)
            if ticket.lease_owner == owner:
                self._put(ticket.model_copy(update={"lease_owner": None, "lease_until": None}))

    async def events(self, ticket_id: str) -> list[Event]:
        self._get(ticket_id)
        return [e.model_copy(deep=True) for e in self._events.get(ticket_id, [])]

    async def overdue_approvals(self, now: datetime) -> list[Ticket]:
        waiting = {TicketStatus.AWAITING_PLAN_APPROVAL, TicketStatus.AWAITING_MERGE_APPROVAL}
        return [
            t.model_copy(deep=True)
            for t in self._tickets.values()
            if t.status in waiting
            and t.pending_resume is None
            and t.approval_deadline is not None
            and t.approval_deadline <= now
        ]


def _jsonable(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        elif hasattr(value, "value"):
            out[key] = value.value
        else:
            out[key] = value
    return out
