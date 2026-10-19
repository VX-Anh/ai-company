"""Bản ghi duyệt. Ghi idempotent theo (ticket, loại, vòng): node chạy lại không tạo bản trùng."""

from __future__ import annotations

import asyncio
import uuid
from typing import Protocol

from aico.core import Approval, ApprovalKind, Decision
from aico.db import DictPool


class ApprovalStore(Protocol):
    async def record(
        self,
        ticket_id: str,
        kind: ApprovalKind,
        round: int,
        decision: Decision,
        *,
        actor: str,
        comment: str = "",
    ) -> Approval: ...

    async def get(self, approval_id: str) -> Approval | None: ...


def _new_id() -> str:
    return f"A-{uuid.uuid4().hex[:12]}"


class InMemoryApprovalStore:
    def __init__(self) -> None:
        self._by_id: dict[str, Approval] = {}
        self._lock = asyncio.Lock()

    async def record(
        self,
        ticket_id: str,
        kind: ApprovalKind,
        round: int,
        decision: Decision,
        *,
        actor: str,
        comment: str = "",
    ) -> Approval:
        async with self._lock:
            for existing in self._by_id.values():
                if (existing.ticket_id, existing.kind, existing.round) == (ticket_id, kind, round):
                    return existing
            approval = Approval(
                id=_new_id(),
                ticket_id=ticket_id,
                kind=kind,
                round=round,
                decision=decision,
                actor=actor,
                comment=comment,
            )
            self._by_id[approval.id] = approval
            return approval

    async def get(self, approval_id: str) -> Approval | None:
        return self._by_id.get(approval_id)


class PostgresApprovalStore:
    def __init__(self, pool: DictPool) -> None:
        self._pool = pool

    async def record(
        self,
        ticket_id: str,
        kind: ApprovalKind,
        round: int,
        decision: Decision,
        *,
        actor: str,
        comment: str = "",
    ) -> Approval:
        async with self._pool.connection() as conn:
            await conn.execute(
                "insert into approvals (id, ticket_id, kind, round, decision, actor, comment)"
                " values (%s, %s, %s, %s, %s, %s, %s)"
                " on conflict (ticket_id, kind, round) do nothing",
                (_new_id(), ticket_id, kind.value, round, decision.value, actor, comment),
            )
            cur = await conn.execute(
                "select id, ticket_id, kind, round, decision, actor, comment, at from approvals"
                " where ticket_id = %s and kind = %s and round = %s",
                (ticket_id, kind.value, round),
            )
            row = await cur.fetchone()
            assert row is not None
            return Approval.model_validate(row)

    async def get(self, approval_id: str) -> Approval | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "select id, ticket_id, kind, round, decision, actor, comment, at from approvals"
                " where id = %s",
                (approval_id,),
            )
            row = await cur.fetchone()
            return None if row is None else Approval.model_validate(row)
