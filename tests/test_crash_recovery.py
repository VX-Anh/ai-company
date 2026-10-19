"""Cổng ra GĐ0: tiến trình chết giữa chừng thì ticket chạy tiếp từ checkpoint, không tạo trùng."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from aico.core import TicketStatus
from aico.db import create_pool
from aico.governance import PostgresApprovalStore
from aico.registry import Company
from aico.tickets.postgres import PostgresTicketStore
from aico.tools import ToolClient
from harness import PROJECT, make_harness
from mcp_gitlab.backend import FakeGitLabBackend

pytestmark = pytest.mark.postgres


class ProcessDied(BaseException):
    """Mô phỏng tiến trình bị kill: không phải Exception nên không ai bắt được."""


class DieAfter:
    def __init__(self, inner: ToolClient, tool: str) -> None:
        self._inner = inner
        self._tool = tool
        self.armed = True

    async def call(self, server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        result = await self._inner.call(server, tool, args)
        if self.armed and tool == self._tool:
            self.armed = False
            raise ProcessDied
        return result


async def test_ticket_survives_process_death_after_push(company: Company, pg_clean: str) -> None:
    pool = create_pool(pg_clean)
    await pool.open()
    tickets = PostgresTicketStore(pool)
    approvals = PostgresApprovalStore(pool)
    gitlab = FakeGitLabBackend()
    try:
        async with AsyncPostgresSaver.from_conn_string(pg_clean) as saver1:
            await saver1.setup()
            h1 = make_harness(
                company,
                tickets=tickets,
                approvals=approvals,
                checkpointer=saver1,
                gitlab=gitlab,
                owner="dead",
                lease_seconds=0.5,
                wrap_client=lambda c: DieAfter(c, "push"),
            )
            await h1.new_ticket(labels=["low-risk"])
            claimed = await tickets.claim(owner="dead", lease_seconds=0.5)
            assert claimed is not None
            with pytest.raises(ProcessDied):
                await h1.runner._process(claimed)  # noqa: SLF001 - chết trước khi nhả lease

        assert gitlab.calls.count("commit") == 1
        assert (await tickets.get("T-1")).lease_owner == "dead"
        await asyncio.sleep(0.8)  # lease hết hạn

        async with AsyncPostgresSaver.from_conn_string(pg_clean) as saver2:
            h2 = make_harness(
                company,
                tickets=tickets,
                approvals=approvals,
                checkpointer=saver2,
                gitlab=gitlab,
                owner="fresh",
            )
            await h2.runner.run_until_idle()
            t = await tickets.get("T-1")
            assert t.status is TicketStatus.AWAITING_MERGE_APPROVAL
            assert gitlab.calls.count("commit") == 1, "push chạy lại phải nhận ra commit cũ"
            assert len(gitlab.project(PROJECT).mrs) == 1

            await h2.decide("T-1", "approved")
            await h2.runner.run_until_idle()
            assert (await tickets.get("T-1")).status is TicketStatus.DONE
    finally:
        await pool.close()
