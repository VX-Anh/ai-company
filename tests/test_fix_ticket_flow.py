from __future__ import annotations

from datetime import timedelta

import pytest

from aico.core import ApprovalKind, Decision, TicketStatus, utcnow
from aico.orchestrator import ReviewResult, ScriptedReviewer, expire_overdue_approvals
from aico.registry import Company
from aico.tools import ApprovalRequired, ToolDenied, ToolError
from harness import PROJECT, make_harness

S = TicketStatus


async def test_low_risk_ticket_skips_plan_approval_and_merges(company: Company) -> None:
    h = make_harness(company)
    await h.new_ticket(labels=["low-risk"])

    await h.runner.run_until_idle()
    t = await h.tickets.get("T-1")
    assert t.status is S.AWAITING_MERGE_APPROVAL
    assert t.risk == "low"
    assert t.approval_deadline is not None
    assert {a.kind for a in t.artifacts} == {"branch", "merge_request"}

    await h.decide("T-1", "approved")
    await h.runner.run_until_idle()
    t = await h.tickets.get("T-1")
    assert t.status is S.DONE
    assert h.gitlab.project(PROJECT).mrs[0]["state"] == "merged"
    statuses = [e.to_status for e in await h.tickets.events("T-1") if e.kind == "status"]
    assert statuses == [
        S.TRIAGED,
        S.PLANNED,
        S.IN_PROGRESS,
        S.IN_REVIEW,
        S.AWAITING_MERGE_APPROVAL,
        S.MERGING,
        S.DONE,
    ]


async def test_medium_risk_waits_for_plan_approval(company: Company) -> None:
    h = make_harness(company)
    await h.new_ticket()

    await h.runner.run_until_idle()
    assert (await h.tickets.get("T-1")).status is S.AWAITING_PLAN_APPROVAL
    # Chưa có quyết định thì không replica nào nhận ticket.
    assert await h.runner.run_once() is None

    await h.decide("T-1", "revise", "thêm test")
    await h.runner.run_until_idle()
    assert (await h.tickets.get("T-1")).status is S.AWAITING_PLAN_APPROVAL

    await h.decide("T-1", "approved")
    await h.runner.run_until_idle()
    assert (await h.tickets.get("T-1")).status is S.AWAITING_MERGE_APPROVAL
    content = h.gitlab.project(PROJECT).files["ai/T-1"]["notes/T-1.md"]
    assert "thêm test" in content


async def test_plan_revisions_are_capped(company: Company) -> None:
    h = make_harness(company)
    await h.new_ticket()
    await h.runner.run_until_idle()
    for _ in range(company.sop("fix_ticket").limits.max_plan_rounds):
        await h.decide("T-1", "revise", "chưa ổn")
        await h.runner.run_until_idle()
    t = await h.tickets.get("T-1")
    assert t.status is S.ESCALATED
    reasons = [e.data.get("reason") for e in await h.tickets.events("T-1") if e.kind == "status"]
    assert any("quá 2 vòng" in str(r) for r in reasons)


async def test_review_failures_loop_back_then_escalate(company: Company) -> None:
    reviewer = ScriptedReviewer([ReviewResult(False, "thiếu test")] * 3)
    h = make_harness(company, reviewer=reviewer)
    await h.new_ticket(labels=["low-risk"])
    await h.runner.run_until_idle()
    t = await h.tickets.get("T-1")
    assert t.status is S.ESCALATED
    # Mỗi vòng code mang idempotency key riêng nên có 3 commit, nhưng chỉ một MR.
    assert h.gitlab.calls.count("commit") == 3
    assert len(h.gitlab.project(PROJECT).mrs) == 1


async def test_merge_rejected_escalates(company: Company) -> None:
    h = make_harness(company)
    await h.new_ticket(labels=["low-risk"])
    await h.runner.run_until_idle()
    await h.decide("T-1", "rejected", "không cần sửa nữa")
    await h.runner.run_until_idle()
    assert (await h.tickets.get("T-1")).status is S.ESCALATED
    assert h.gitlab.project(PROJECT).mrs[0]["state"] == "opened"


async def test_merge_failure_retries_coding(company: Company) -> None:
    h = make_harness(company)
    h.gitlab.merge_fails = 1
    await h.new_ticket(labels=["low-risk"])
    await h.runner.run_until_idle()
    await h.decide("T-1", "approved")
    await h.runner.run_until_idle()
    # Merge lỗi lần đầu: quay lại code, review, rồi chờ duyệt merge vòng 2.
    assert (await h.tickets.get("T-1")).status is S.AWAITING_MERGE_APPROVAL
    await h.decide("T-1", "approved")
    await h.runner.run_until_idle()
    assert (await h.tickets.get("T-1")).status is S.DONE


async def test_overdue_approval_times_out_to_escalated(company: Company) -> None:
    h = make_harness(company)
    await h.new_ticket()
    await h.runner.run_until_idle()
    await h.tickets.update("T-1", actor="test", approval_deadline=utcnow() - timedelta(minutes=1))
    assert await expire_overdue_approvals(h.tickets) == ["T-1"]
    await h.runner.run_until_idle()
    assert (await h.tickets.get("T-1")).status is S.ESCALATED


async def test_merge_needs_a_valid_approval_of_this_ticket(company: Company) -> None:
    h = make_harness(company)
    await h.new_ticket(labels=["low-risk"])
    await h.runner.run_until_idle()
    args = {"project": PROJECT, "mr_iid": 1}
    with pytest.raises(ApprovalRequired):
        await h.gateway.call("merge_bot", "gitlab", "merge_mr", args, ticket_id="T-1")
    # Bản ghi duyệt của ticket khác bị guard chặn.
    other = await h.approvals.record("T-9", ApprovalKind.MERGE, 1, Decision.APPROVED, actor="o")
    with pytest.raises(ToolDenied):
        await h.gateway.call(
            "merge_bot", "gitlab", "merge_mr", {**args, "approval_id": other.id}, ticket_id="T-1"
        )
    # Kể cả khi vượt qua guard (gọi thẳng với ticket_id giả), MCP server vẫn tự kiểm tra MR.
    with pytest.raises(ToolError):
        await h.gateway.call(
            "merge_bot", "gitlab", "merge_mr", {**args, "approval_id": other.id}, ticket_id="T-9"
        )
    assert h.gitlab.project(PROJECT).mrs[0]["state"] == "opened"
