"""Vòng chạy ticket: nhận lease, chạy graph tới điểm dừng, nhả lease.

Runner (không phải node) chuyển ticket sang trạng thái chờ duyệt khi graph dừng ở interrupt,
vì lúc đó checkpoint đã lưu xong.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from aico.core import Ticket, TicketStatus, utcnow
from aico.tickets import TicketStore

log = logging.getLogger(__name__)

CallbacksFactory = Callable[[Ticket], list[Any]]


class Runner:
    def __init__(
        self,
        graph: CompiledStateGraph[Any, Any, Any, Any],
        tickets: TicketStore,
        *,
        owner: str | None = None,
        lease_seconds: float = 60,
        callbacks: CallbacksFactory | None = None,
    ) -> None:
        self._graph = graph
        self._tickets = tickets
        self.owner = owner or f"aico-{uuid.uuid4().hex[:8]}"
        self._lease = lease_seconds
        self._callbacks = callbacks

    async def run_once(self) -> Ticket | None:
        """Nhận và chạy một ticket. Trả ticket đã xử lý, hoặc None nếu không có việc."""
        ticket = await self._tickets.claim(owner=self.owner, lease_seconds=self._lease)
        if ticket is None:
            return None
        heartbeat = asyncio.create_task(self._heartbeat(ticket.id))
        try:
            await self._process(ticket)
        except Exception as exc:
            log.exception("ticket %s lỗi", ticket.id)
            current = await self._tickets.get(ticket.id)
            if not current.is_terminal:
                await self._tickets.transition(
                    ticket.id,
                    TicketStatus.FAILED,
                    actor=self.owner,
                    data={"error": f"{type(exc).__name__}: {exc}"},
                )
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            await self._tickets.release(ticket.id, owner=self.owner)
        return await self._tickets.get(ticket.id)

    async def run_until_idle(self, max_steps: int = 100) -> int:
        steps = 0
        while steps < max_steps and await self.run_once() is not None:
            steps += 1
        return steps

    async def run_forever(self, idle_seconds: float = 2.0) -> None:
        while True:
            if await self.run_once() is None:
                await asyncio.sleep(idle_seconds)

    async def _heartbeat(self, ticket_id: str) -> None:
        while True:
            await asyncio.sleep(self._lease / 3)
            if not await self._tickets.renew(
                ticket_id, owner=self.owner, lease_seconds=self._lease
            ):
                log.warning("mất lease ticket %s", ticket_id)
                return

    async def _process(self, ticket: Ticket) -> None:
        config: RunnableConfig = {"configurable": {"thread_id": ticket.id}}
        if self._callbacks is not None:
            config["callbacks"] = self._callbacks(ticket)
        resume = await self._tickets.take_pending_resume(ticket.id, owner=self.owner)
        snapshot = await self._graph.aget_state(config)
        payload: Any
        if resume is not None:
            payload = Command(resume=resume)
        elif snapshot.next:
            payload = None  # chạy tiếp từ checkpoint cuối sau khi tiến trình chết
        else:
            payload = {"ticket_id": ticket.id}
        result = await self._graph.ainvoke(payload, config)
        for item in result.get("__interrupt__", ()):
            value = item.value
            data = {"kind": value["kind"], "round": value["round"]}
            # Người duyệt đọc yêu cầu, kế hoạch, ghi chú review / kiểm thử qua /events.
            for key in ("requirements", "plan", "work_items", "review_notes", "test_notes"):
                if value.get(key):
                    data[key] = value[key]
            await self._tickets.transition(
                ticket.id,
                TicketStatus(value["status"]),
                actor=self.owner,
                data=data,
            )
            deadline = utcnow() + timedelta(hours=float(value["deadline_hours"]))
            await self._tickets.update(ticket.id, actor=self.owner, approval_deadline=deadline)


async def expire_overdue_approvals(tickets: TicketStore) -> list[str]:
    """Điểm chờ duyệt quá hạn: gửi quyết định timeout để graph chuyển escalate."""
    expired: list[str] = []
    for ticket in await tickets.overdue_approvals(utcnow()):
        await tickets.set_pending_resume(
            ticket.id, {"decision": "timeout", "actor": "scheduler"}, actor="scheduler"
        )
        expired.append(ticket.id)
    return expired
