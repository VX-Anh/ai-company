"""Dựng đủ hệ thống trong bộ nhớ để test luồng: GitLab giả, MCP server thật, graph, runner."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from aico.core import Ticket
from aico.governance import ApprovalStore, InMemoryApprovalStore, InMemoryAuditLog
from aico.governance.verify import verify_merge_approval
from aico.orchestrator import (
    Deps,
    RuleTriage,
    Runner,
    ScriptedCoder,
    ScriptedReviewer,
    StaticPlanner,
    build_fix_ticket,
)
from aico.orchestrator.ports import Analyst, Coder, Planner, Previewer, Reviewer
from aico.registry import Company
from aico.tickets import InMemoryTicketStore, TicketStore
from aico.tools import Guard, McpToolClient, ToolClient, ToolGateway
from mcp_gitlab.backend import FakeGitLabBackend
from mcp_gitlab.server import build_server
from mcp_gitlab.tools import Constraints, GitLabTools

PROJECT = "VX-Anh/aico-office"


@dataclass
class Harness:
    company: Company
    tickets: TicketStore
    approvals: ApprovalStore
    audit: InMemoryAuditLog
    gitlab: FakeGitLabBackend
    reviewer: ScriptedReviewer
    gateway: ToolGateway
    runner: Runner

    async def new_ticket(self, ticket_id: str = "T-1", **kw: object) -> Ticket:
        ticket = Ticket(id=ticket_id, title="Sửa lỗi hiển thị", project=PROJECT, **kw)  # type: ignore[arg-type]
        return await self.tickets.create(ticket, actor="test")

    async def decide(self, ticket_id: str, decision: str, comment: str = "") -> None:
        await self.tickets.set_pending_resume(
            ticket_id, {"decision": decision, "actor": "owner", "comment": comment}, actor="owner"
        )


def make_harness(
    company: Company,
    *,
    tickets: TicketStore | None = None,
    approvals: ApprovalStore | None = None,
    checkpointer: BaseCheckpointSaver[str] | None = None,
    gitlab: FakeGitLabBackend | None = None,
    reviewer: ScriptedReviewer | None = None,
    owner: str | None = None,
    lease_seconds: float = 30,
    wrap_client: Callable[[ToolClient], ToolClient] | None = None,
    planner: Callable[[ToolGateway], Planner] | None = None,
    coder: Callable[[ToolGateway], Coder] | None = None,
    reviewer_agent: Callable[[ToolGateway], Reviewer] | None = None,
    previewer: Previewer | None = None,
    analyst: Analyst | None = None,
    tester: Reviewer | None = None,
    label_item_roles: dict[str, tuple[str, ...]] | None = None,
    test_skip_labels: tuple[str, ...] = (),
) -> Harness:
    tickets = tickets or InMemoryTicketStore()
    approvals = approvals or InMemoryApprovalStore()
    gitlab = gitlab or FakeGitLabBackend()
    gitlab.project(PROJECT)
    reviewer = reviewer or ScriptedReviewer()
    audit = InMemoryAuditLog()

    async def verify(approval_id: str, project: str, mr_iid: int) -> tuple[bool, str]:
        return await verify_merge_approval(approvals, tickets, approval_id, project, mr_iid)

    server = build_server(GitLabTools(gitlab, Constraints(allowed_projects=(PROJECT,)), verify))
    client: ToolClient = McpToolClient({"gitlab": server})
    if wrap_client is not None:
        client = wrap_client(client)
    gateway = ToolGateway(company, Guard(company.policy, approvals), client, audit)
    sop = company.sop("fix_ticket")
    deps = Deps(
        sop=sop,
        tickets=tickets,
        approvals=approvals,
        gateway=gateway,
        triage=RuleTriage(company.policy, sop.department),
        planner=planner(gateway) if planner is not None else StaticPlanner(),
        coder=coder(gateway) if coder is not None else ScriptedCoder(),
        reviewer=reviewer_agent(gateway) if reviewer_agent is not None else reviewer,
        default_project=PROJECT,
        item_roles=("coder", "designer"),
        previewer=previewer,
        git_retry_delays=(0.0,),  # test không chờ giữa các lần thử lại Git
        analyst=analyst,
        analyze_skip_labels=("retro", "docs"),
        tester=tester,
        label_item_roles=label_item_roles or {},
        test_skip_labels=test_skip_labels,
    )
    graph = build_fix_ticket(deps).compile(checkpointer=checkpointer or InMemorySaver())
    runner = Runner(graph, tickets, owner=owner, lease_seconds=lease_seconds)
    return Harness(company, tickets, approvals, audit, gitlab, reviewer, gateway, runner)


def text_files(files: Mapping[str, str | bytes]) -> dict[str, str]:
    """Bản text các file một branch của backend giả (file nhị phân giải mã utf-8)."""
    return {path: c.decode("utf-8") if isinstance(c, bytes) else c for path, c in files.items()}
