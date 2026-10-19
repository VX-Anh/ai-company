"""Sổ ngân sách: giữ chỗ trước mỗi lời gọi model, đối soát bằng usage thật sau đó.

9router chỉ có hạn mức theo provider, nên trần theo ticket và theo vai trò thực thi ở đây.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from aico.core import PolicySpec, RoleSpec, utcnow
from aico.db import DictPool


class BudgetExceeded(Exception):
    pass


@dataclass(frozen=True)
class Reservation:
    id: str
    ticket_id: str
    role: str
    model: str
    est_usd: float
    est_tokens: int


def estimate_tokens(body: dict[str, Any], default_max_tokens: int = 4096) -> int:
    """Ước lượng thô: ~3 ký tự một token cho phần vào, cộng trần token đầu ra."""
    prompt_chars = len(str(body.get("messages", "")))
    max_out = body.get("max_completion_tokens") or body.get("max_tokens") or default_max_tokens
    return math.ceil(prompt_chars / 3) + int(max_out)


def cost_usd(policy: PolicySpec, model: str, prompt_tokens: int, completion_tokens: int) -> float:
    price = policy.model_prices.get(model)
    if price is None:
        return 0.0
    return (prompt_tokens * price.input_per_mtok + completion_tokens * price.output_per_mtok) / 1e6


def estimate_usd(policy: PolicySpec, model: str, tokens: int) -> float:
    price = policy.model_prices.get(model)
    if price is None:
        return 0.0
    return tokens * max(price.input_per_mtok, price.output_per_mtok) / 1e6


def _check(
    role: RoleSpec,
    *,
    ticket_budget: float,
    ticket_usd: float,
    role_ticket_usd: float,
    role_ticket_tokens: int,
    role_month_usd: float,
    est_usd: float,
    est_tokens: int,
) -> None:
    if ticket_usd + est_usd > ticket_budget:
        raise BudgetExceeded(f"vượt ngân sách ticket ({ticket_budget} USD)")
    if role.budget is None:
        return
    if role_ticket_usd + est_usd > role.budget.per_ticket_usd:
        raise BudgetExceeded(
            f"vượt ngân sách {role.id} cho ticket ({role.budget.per_ticket_usd} USD)"
        )
    limit = role.budget.per_ticket_tokens
    if limit is not None and role_ticket_tokens + est_tokens > limit:
        raise BudgetExceeded(f"vượt trần token {role.id} cho ticket ({limit})")
    if role_month_usd + est_usd > role.budget.monthly_usd:
        raise BudgetExceeded(f"vượt ngân sách tháng của {role.id} ({role.budget.monthly_usd} USD)")


class BudgetLedger(Protocol):
    async def reserve(
        self, ticket_id: str, role: RoleSpec, model: str, est_tokens: int, est_usd: float
    ) -> Reservation: ...

    async def settle(self, reservation: Reservation, tokens: int, usd: float) -> None: ...

    async def cancel(self, reservation: Reservation) -> None: ...


@dataclass
class _Entry:
    r: Reservation
    status: str = "open"
    actual_usd: float = 0.0
    actual_tokens: int = 0
    at: datetime = field(default_factory=utcnow)

    @property
    def usd(self) -> float:
        return self.r.est_usd if self.status == "open" else self.actual_usd

    @property
    def tokens(self) -> int:
        return self.r.est_tokens if self.status == "open" else self.actual_tokens


class InMemoryBudgetLedger:
    def __init__(
        self, ticket_budgets: dict[str, float] | None = None, default: float = 5.0
    ) -> None:
        self._budgets = ticket_budgets or {}
        self._default = default
        self._entries: list[_Entry] = []
        self._lock = asyncio.Lock()

    def spent(self, ticket_id: str) -> tuple[float, int]:
        live = [e for e in self._entries if e.r.ticket_id == ticket_id and e.status != "cancelled"]
        return sum(e.usd for e in live), sum(e.tokens for e in live)

    async def reserve(
        self, ticket_id: str, role: RoleSpec, model: str, est_tokens: int, est_usd: float
    ) -> Reservation:
        async with self._lock:
            live = [e for e in self._entries if e.status != "cancelled"]
            now = utcnow()
            mine = [e for e in live if e.r.ticket_id == ticket_id]
            role_ticket = [e for e in mine if e.r.role == role.id]
            month = [
                e
                for e in live
                if e.r.role == role.id and (e.at.year, e.at.month) == (now.year, now.month)
            ]
            _check(
                role,
                ticket_budget=self._budgets.get(ticket_id, self._default),
                ticket_usd=sum(e.usd for e in mine),
                role_ticket_usd=sum(e.usd for e in role_ticket),
                role_ticket_tokens=sum(e.tokens for e in role_ticket),
                role_month_usd=sum(e.usd for e in month),
                est_usd=est_usd,
                est_tokens=est_tokens,
            )
            r = Reservation(
                f"R-{uuid.uuid4().hex[:12]}", ticket_id, role.id, model, est_usd, est_tokens
            )
            self._entries.append(_Entry(r))
            return r

    async def settle(self, reservation: Reservation, tokens: int, usd: float) -> None:
        async with self._lock:
            for e in self._entries:
                if e.r.id == reservation.id and e.status == "open":
                    e.status, e.actual_tokens, e.actual_usd = "settled", tokens, usd

    async def cancel(self, reservation: Reservation) -> None:
        async with self._lock:
            for e in self._entries:
                if e.r.id == reservation.id and e.status == "open":
                    e.status = "cancelled"


_SPENT = (
    "coalesce(sum(case when status = 'settled' then actual_usd else est_usd end), 0) as usd,"
    " coalesce(sum(case when status = 'settled' then actual_tokens else est_tokens end), 0)"
    " as tokens"
)


class PostgresBudgetLedger:
    def __init__(self, pool: DictPool) -> None:
        self._pool = pool

    async def reserve(
        self, ticket_id: str, role: RoleSpec, model: str, est_tokens: int, est_usd: float
    ) -> Reservation:
        async with self._pool.connection() as conn, conn.transaction():
            # Khóa dòng ticket: các lời gọi đồng thời của cùng ticket xếp hàng ở đây.
            cur = await conn.execute(
                "select budget_usd from tickets where id = %s for update", (ticket_id,)
            )
            row = await cur.fetchone()
            if row is None:
                raise BudgetExceeded(f"không có ticket {ticket_id}")
            live = "status in ('open', 'settled')"
            ticket = await (
                await conn.execute(
                    f"select {_SPENT} from budget_reservations where ticket_id = %s and {live}",
                    (ticket_id,),
                )
            ).fetchone()
            role_ticket = await (
                await conn.execute(
                    f"select {_SPENT} from budget_reservations"
                    f" where ticket_id = %s and role = %s and {live}",
                    (ticket_id, role.id),
                )
            ).fetchone()
            month = await (
                await conn.execute(
                    f"select {_SPENT} from budget_reservations where role = %s and {live}"
                    " and created_at >= date_trunc('month', now())",
                    (role.id,),
                )
            ).fetchone()
            assert ticket is not None and role_ticket is not None and month is not None
            _check(
                role,
                ticket_budget=row["budget_usd"],
                ticket_usd=ticket["usd"],
                role_ticket_usd=role_ticket["usd"],
                role_ticket_tokens=role_ticket["tokens"],
                role_month_usd=month["usd"],
                est_usd=est_usd,
                est_tokens=est_tokens,
            )
            r = Reservation(
                f"R-{uuid.uuid4().hex[:12]}", ticket_id, role.id, model, est_usd, est_tokens
            )
            await conn.execute(
                "insert into budget_reservations (id, ticket_id, role, model, est_usd, est_tokens,"
                " status) values (%s, %s, %s, %s, %s, %s, 'open')",
                (r.id, ticket_id, role.id, model, est_usd, est_tokens),
            )
            return r

    async def settle(self, reservation: Reservation, tokens: int, usd: float) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "update budget_reservations set status = 'settled', actual_usd = %s,"
                " actual_tokens = %s, settled_at = now() where id = %s and status = 'open'",
                (usd, tokens, reservation.id),
            )
            if cur.rowcount == 1:
                await conn.execute(
                    "update tickets set spent_usd = spent_usd + %s,"
                    " spent_tokens = spent_tokens + %s where id = %s",
                    (usd, tokens, reservation.ticket_id),
                )

    async def cancel(self, reservation: Reservation) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "update budget_reservations set status = 'cancelled', settled_at = now()"
                " where id = %s and status = 'open'",
                (reservation.id,),
            )
