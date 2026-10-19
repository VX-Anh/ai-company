"""Nối các thành phần lại. GĐ0 dùng planner, coder, reviewer giả lập; GĐ1 thay bằng agent thật."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from aico.api import ApiDeps
from aico.db import create_pool
from aico.governance import PostgresApprovalStore, PostgresAuditLog
from aico.intake import GitLabIssuePoller
from aico.llm_relay import PostgresBudgetLedger, RelayDeps, TaskTokens
from aico.observability import LoggingTraceSink
from aico.orchestrator import (
    Deps,
    RuleTriage,
    Runner,
    ScriptedCoder,
    ScriptedReviewer,
    StaticPlanner,
    build_fix_ticket,
)
from aico.registry import Company, load_company
from aico.settings import Settings
from aico.tickets.postgres import PostgresTicketStore
from aico.tools import Guard, McpToolClient, ToolGateway


@dataclass(frozen=True)
class Container:
    settings: Settings
    company: Company
    runner: Runner
    poller: GitLabIssuePoller | None
    api: ApiDeps
    relay: RelayDeps
    tickets: PostgresTicketStore


@contextlib.asynccontextmanager
async def build_container(settings: Settings) -> AsyncIterator[Container]:
    company = load_company(settings.company_dir)
    pool = create_pool(settings.database_url)
    # Checkpointer của LangGraph cần autocommit và tắt prepared statement.
    saver_pool: AsyncConnectionPool = AsyncConnectionPool(
        settings.database_url,
        max_size=5,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )
    headers = (
        {"Authorization": f"Bearer {settings.upstream_llm_key}"}
        if settings.upstream_llm_key
        else {}
    )
    upstream = httpx.AsyncClient(
        base_url=settings.upstream_llm_url.rstrip("/") + "/", headers=headers, timeout=300
    )
    await pool.open()
    await saver_pool.open()
    try:
        tickets = PostgresTicketStore(pool)
        approvals = PostgresApprovalStore(pool)
        gateway = ToolGateway(
            company,
            Guard(company.policy, approvals),
            McpToolClient({"gitlab": settings.mcp_gitlab_url}),
            PostgresAuditLog(pool),
        )
        sop = company.sop("fix_ticket")
        deps = Deps(
            sop=sop,
            tickets=tickets,
            approvals=approvals,
            gateway=gateway,
            triage=RuleTriage(company.policy, sop.department),
            planner=StaticPlanner(),
            coder=ScriptedCoder(),
            reviewer=ScriptedReviewer(),
            default_project=settings.default_project or "group/pilot-repo",
        )
        saver = AsyncPostgresSaver(saver_pool)  # type: ignore[arg-type]
        graph = build_fix_ticket(deps).compile(checkpointer=saver)
        runner = Runner(graph, tickets, lease_seconds=settings.lease_seconds)
        poller = (
            GitLabIssuePoller(
                gateway, tickets, settings.intake_projects, label=settings.intake_label
            )
            if settings.intake_projects
            else None
        )
        yield Container(
            settings=settings,
            company=company,
            runner=runner,
            poller=poller,
            api=ApiDeps(
                tickets=tickets,
                approvals=approvals,
                api_token=settings.api_token,
                internal_token=settings.internal_token,
                default_project=settings.default_project,
            ),
            relay=RelayDeps(
                company=company,
                tokens=TaskTokens(),
                ledger=PostgresBudgetLedger(pool),
                upstream=upstream,
                trace=LoggingTraceSink(),
            ),
            tickets=tickets,
        )
    finally:
        await upstream.aclose()
        await saver_pool.close()
        await pool.close()


async def setup_checkpointer(database_url: str) -> None:
    async with AsyncPostgresSaver.from_conn_string(database_url) as saver:
        await saver.setup()
