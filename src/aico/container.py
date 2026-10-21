"""Nối các thành phần lại. GĐ0 dùng planner, coder, reviewer giả lập; GĐ1 thay bằng agent thật."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from starlette.applications import Starlette

from aico.agents import (
    REMOTE_BUILD_HINT,
    RETRO_LABEL,
    CliAgentRunner,
    CliCoder,
    CliReviewer,
    CliSettings,
    LlmAgentRunner,
    LlmPlanner,
    LlmSettings,
    PreviewSettings,
    RemotePreviewer,
    RetroPlanner,
)
from aico.api import ApiDeps
from aico.core import Ticket
from aico.db import create_pool
from aico.governance import PostgresApprovalStore, PostgresAuditLog
from aico.intake import GitLabIssuePoller
from aico.llm_relay import PostgresBudgetLedger, RelayDeps, TaskTokens, create_relay_app
from aico.observability import LoggingTraceSink
from aico.orchestrator import (
    Deps,
    LabelPlanner,
    RuleTriage,
    Runner,
    ScriptedCoder,
    ScriptedReviewer,
    StaticPlanner,
    build_fix_ticket,
)
from aico.orchestrator.escalation import merge_escalated
from aico.orchestrator.ports import Coder, Planner, Reviewer
from aico.registry import Company, load_company
from aico.settings import Settings
from aico.tickets.postgres import PostgresTicketStore
from aico.tools import Guard, McpToolClient, ToolGateway
from aico.tools.bridge import ToolBridge, create_tool_bridge_app

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Container:
    settings: Settings
    company: Company
    runner: Runner
    # Nhiều vòng runner, mỗi vòng giữ một ticket: các ticket chạy song song ở các phòng provider.
    runners: tuple[Runner, ...]
    poller: GitLabIssuePoller | None
    api: ApiDeps
    relay_app: FastAPI
    bridge_app: Starlette
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
    exit_stack = contextlib.AsyncExitStack()
    try:
        tickets = PostgresTicketStore(pool)
        approvals = PostgresApprovalStore(pool)
        gateway = ToolGateway(
            company,
            Guard(company.policy, approvals),
            McpToolClient(
                {"gitlab": settings.mcp_gitlab_url}
                | ({"pixel": settings.mcp_pixel_url} if settings.mcp_pixel_url else {})
            ),
            PostgresAuditLog(pool),
        )
        tokens = TaskTokens()
        relay_app = create_relay_app(
            RelayDeps(
                company=company,
                tokens=tokens,
                ledger=PostgresBudgetLedger(pool),
                upstream=upstream,
                trace=LoggingTraceSink(),
            )
        )
        # Agent trong aico gọi relay ngay trong tiến trình; sandbox (GĐ1) gọi qua mạng.
        relay_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=relay_app), base_url="http://relay", timeout=300
        )
        exit_stack.push_async_callback(relay_client.aclose)
        sop = company.sop("fix_ticket")
        # Tool bridge cho worker CLI: token riêng, không dùng chung với model relay.
        bridge_tokens = TaskTokens()
        bridge_app = create_tool_bridge_app(ToolBridge(gateway, bridge_tokens))
        planner: Planner = StaticPlanner()
        cli_agents: CliAgentRunner | None = None
        uses_cli = "cli" in (settings.planner, settings.coder, settings.reviewer)
        if uses_cli and (not settings.workers or not settings.worker_token):
            raise RuntimeError("worker cli cần AICO_WORKERS và AICO_WORKER_TOKEN_FILE")
        if settings.workers and settings.worker_token:
            worker_client = httpx.AsyncClient()
            exit_stack.push_async_callback(worker_client.aclose)
            cli_agents = CliAgentRunner(
                company,
                bridge_tokens,
                CliSettings(
                    workers=settings.workers,
                    worker_token=settings.worker_token,
                    bridge_url=settings.bridge_url,
                    usage_tight_percent=settings.usage_tight_percent,
                    http_client=worker_client,
                ),
            )
        if settings.planner == "cli" and cli_agents is not None:
            planner = LlmPlanner(cli_agents, role_id=sop.steps["plan"])
        elif settings.planner == "llm":
            agents = LlmAgentRunner(
                company,
                gateway,
                tokens,
                LlmSettings(relay_url="http://relay/v1", http_client=relay_client),
            )
            planner = LlmPlanner(agents, role_id=sop.steps["plan"])
        if cli_agents is not None and "researcher" in company.roles:
            # Ticket gắn nhãn retro: Researcher hồi cứu lịch sử thay Tech Lead lập kế hoạch.
            planner = LabelPlanner(planner, {RETRO_LABEL: RetroPlanner(cli_agents, tickets)})
        remote = await _remote_ready(settings) if settings.worker_token else False
        build_hint = REMOTE_BUILD_HINT if remote else None
        previewer: RemotePreviewer | None = None
        if remote and settings.remote_url and settings.worker_token and settings.preview_projects:
            preview_client = httpx.AsyncClient()
            exit_stack.push_async_callback(preview_client.aclose)
            previewer = RemotePreviewer(
                PreviewSettings(
                    url=settings.remote_url,
                    token=settings.worker_token,
                    http_client=preview_client,
                    projects=settings.preview_projects,
                ),
                gateway,
                tickets,
                role_id=sop.steps["code"],
            )
        coder: Coder = ScriptedCoder()
        if settings.coder == "cli" and cli_agents is not None:
            coder = CliCoder(
                cli_agents,
                gateway,
                role_id=sop.steps["code"],
                default_project=settings.default_project,
                build_hint=build_hint,
            )
        reviewer: Reviewer = ScriptedReviewer()
        if settings.reviewer == "cli" and cli_agents is not None:
            reviewer = CliReviewer(
                cli_agents, gateway, role_id=sop.steps["review"], build_hint=build_hint
            )
        deps = Deps(
            sop=sop,
            tickets=tickets,
            approvals=approvals,
            gateway=gateway,
            triage=RuleTriage(company.policy, sop.department),
            planner=planner,
            coder=coder,
            reviewer=reviewer,
            default_project=settings.default_project or "group/pilot-repo",
            item_roles=("coder", "designer"),
            previewer=previewer,
        )

        async def preview_ticket(ticket_id: str) -> dict[str, Any]:
            if previewer is None:
                raise ValueError("chưa bật bản xem trước (AICO_PREVIEW_PROJECTS)")
            ticket = await tickets.get(ticket_id)
            branches = [a.ref for a in ticket.artifacts if a.kind == "branch"]
            project = ticket.project or settings.default_project
            if not branches or not project:
                raise ValueError("ticket chưa có branch code")
            if not previewer.enabled_for(project):
                raise ValueError(f"repo {project} không nằm trong AICO_PREVIEW_PROJECTS")
            return await previewer.build(ticket_id, project, branches[-1])

        async def merge_escalated_ticket(ticket_id: str, actor: str, comment: str) -> Ticket:
            return await merge_escalated(
                tickets,
                approvals,
                gateway,
                ticket_id,
                actor=actor,
                comment=comment,
                merge_role=sop.steps["merge"],
            )

        saver = AsyncPostgresSaver(saver_pool)  # type: ignore[arg-type]
        graph = build_fix_ticket(deps).compile(checkpointer=saver)
        runners = tuple(
            Runner(graph, tickets, lease_seconds=settings.lease_seconds)
            for _ in range(settings.runner_concurrency)
        )
        runner = runners[0]
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
            runners=runners,
            poller=poller,
            api=ApiDeps(
                tickets=tickets,
                approvals=approvals,
                api_token=settings.api_token,
                internal_token=settings.internal_token,
                default_project=settings.default_project,
                workers=cli_agents.statuses if cli_agents is not None else None,
                cors_origins=settings.cors_origins,
                cors_origin_regex=settings.cors_origin_regex,
                merge_escalated=merge_escalated_ticket,
                preview=preview_ticket if previewer is not None else None,
            ),
            relay_app=relay_app,
            bridge_app=bridge_app,
            tickets=tickets,
        )
    finally:
        await exit_stack.aclose()
        await upstream.aclose()
        await saver_pool.close()
        await pool.close()


async def _remote_ready(settings: Settings) -> bool:
    """Máy build từ xa đã cấu hình server chưa; chưa thì agent chạy build/test như cũ."""
    if not settings.remote_url:
        return False
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.remote_url.rstrip('/')}/health")
            return resp.status_code == 200 and bool(resp.json().get("target"))
    except (httpx.HTTPError, ValueError):
        log.warning("không gọi được máy build từ xa %s", settings.remote_url)
        return False


async def setup_checkpointer(database_url: str) -> None:
    async with AsyncPostgresSaver.from_conn_string(database_url) as saver:
        await saver.setup()
