from __future__ import annotations

import pytest

from aico.core import ActionLevel, ApprovalKind, Decision
from aico.governance import InMemoryApprovalStore, InMemoryAuditLog
from aico.registry import Company
from aico.tools import (
    ApprovalRequired,
    Guard,
    McpToolClient,
    ToolDenied,
    ToolError,
    ToolGateway,
    Verdict,
    check_branch,
)
from mcp_gitlab.backend import FakeGitLabBackend
from mcp_gitlab.server import build_server
from mcp_gitlab.tools import Constraints, GitLabTools

PROJECT = "VX-Anh/aico-office"


def test_check_branch() -> None:
    assert check_branch("ai/T-1", "ai/*")
    assert not check_branch("main", "ai/*")
    assert not check_branch("ai/../main", "ai/*")
    assert not check_branch("ai/", "ai/*")


async def test_guard_rules(company: Company) -> None:
    approvals = InMemoryApprovalStore()
    guard = Guard(company.policy, approvals)
    coder = company.role("coder")
    tech_lead = company.role("tech_lead")
    merge_bot = company.role("merge_bot")

    ok = await guard.check(
        coder, "gitlab", "push", {"project": PROJECT, "branch": "ai/T-1"}, ticket_id="T-1"
    )
    assert ok.verdict is Verdict.ALLOW and ok.level is ActionLevel.L2

    not_granted = await guard.check(
        tech_lead, "gitlab", "push", {"project": PROJECT}, ticket_id="T-1"
    )
    assert not_granted.verdict is Verdict.DENY

    wrong_repo = await guard.check(
        coder, "gitlab", "read_file", {"project": "x/y"}, ticket_id="T-1"
    )
    assert wrong_repo.verdict is Verdict.DENY

    missing_repo = await guard.check(coder, "gitlab", "read_file", {}, ticket_id="T-1")
    assert missing_repo.verdict is Verdict.DENY

    bad_branch = await guard.check(
        coder, "gitlab", "push", {"project": PROJECT, "branch": "main"}, ticket_id="T-1"
    )
    assert bad_branch.verdict is Verdict.DENY

    no_approval = await guard.check(
        merge_bot, "gitlab", "merge_mr", {"project": PROJECT, "mr_iid": 1}, ticket_id="T-1"
    )
    assert no_approval.verdict is Verdict.NEEDS_APPROVAL

    rejected = await approvals.record(
        "T-1", ApprovalKind.MERGE, 1, Decision.REJECTED, actor="owner"
    )
    assert (
        await guard.check(
            merge_bot,
            "gitlab",
            "merge_mr",
            {"project": PROJECT, "mr_iid": 1, "approval_id": rejected.id},
            ticket_id="T-1",
        )
    ).verdict is Verdict.DENY

    plan_ok = await approvals.record("T-1", ApprovalKind.PLAN, 1, Decision.APPROVED, actor="owner")
    assert (
        await guard.check(
            merge_bot,
            "gitlab",
            "merge_mr",
            {"project": PROJECT, "mr_iid": 1, "approval_id": plan_ok.id},
            ticket_id="T-1",
        )
    ).verdict is Verdict.DENY

    merge_ok = await approvals.record(
        "T-1", ApprovalKind.MERGE, 2, Decision.APPROVED, actor="owner"
    )
    args = {"project": PROJECT, "mr_iid": 1, "approval_id": merge_ok.id}
    assert (await guard.check(merge_bot, "gitlab", "merge_mr", args, ticket_id="T-1")).allowed
    other_ticket = await guard.check(merge_bot, "gitlab", "merge_mr", args, ticket_id="T-2")
    assert other_ticket.verdict is Verdict.DENY


async def test_approvals_are_idempotent_per_round() -> None:
    store = InMemoryApprovalStore()
    a = await store.record("T-1", ApprovalKind.PLAN, 1, Decision.APPROVED, actor="owner")
    b = await store.record("T-1", ApprovalKind.PLAN, 1, Decision.REJECTED, actor="owner")
    assert a.id == b.id and b.decision is Decision.APPROVED


async def test_gateway_end_to_end_with_mcp_server(company: Company) -> None:
    backend = FakeGitLabBackend()
    backend.project(PROJECT).files["main"]["README.md"] = "# pilot\n"
    server = build_server(GitLabTools(backend, Constraints(allowed_projects=(PROJECT,))))
    audit = InMemoryAuditLog()
    gateway = ToolGateway(
        company,
        Guard(company.policy, InMemoryApprovalStore()),
        McpToolClient({"gitlab": server}),
        audit,
    )
    result = await gateway.call(
        "tech_lead",
        "gitlab",
        "read_file",
        {"project": PROJECT, "path": "README.md"},
        ticket_id="T-1",
    )
    assert result["content"] == "# pilot\n"

    with pytest.raises(ToolDenied):
        await gateway.call("tech_lead", "gitlab", "push", {"project": PROJECT}, ticket_id="T-1")
    with pytest.raises(ApprovalRequired):
        await gateway.call(
            "merge_bot", "gitlab", "merge_mr", {"project": PROJECT, "mr_iid": 1}, ticket_id="T-1"
        )
    with pytest.raises(ToolError):
        await gateway.call(
            "tech_lead",
            "gitlab",
            "read_file",
            {"project": PROJECT, "path": "missing.md"},
            ticket_id="T-1",
        )

    decisions = [(e.tool, e.decision, e.error is None) for e in audit.entries]
    assert decisions == [
        ("read_file", "allow", True),
        ("push", "deny", False),
        ("merge_mr", "needs_approval", False),
        ("read_file", "allow", False),
    ]


async def test_audit_masks_secrets(company: Company) -> None:
    from aico.governance import mask_secrets

    masked = mask_secrets({"project": PROJECT, "api_token": "abc", "nested": {"password": "x"}})
    assert masked == {"project": PROJECT, "api_token": "***", "nested": {"password": "***"}}
