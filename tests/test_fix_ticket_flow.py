from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from aico.core import ApprovalKind, Decision, Ticket, TicketStatus, utcnow
from aico.orchestrator import ReviewResult, ScriptedReviewer, expire_overdue_approvals
from aico.registry import Company
from aico.tools import ApprovalRequired, ToolClient, ToolDenied, ToolError, ToolSpec
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


async def test_escalated_ticket_merged_by_merge_bot_after_human_approval(company: Company) -> None:
    import httpx

    from aico.api import ApiDeps, create_api_app
    from aico.orchestrator.escalation import EscalationError, merge_escalated

    reviewer = ScriptedReviewer([ReviewResult(False, "thiếu test")] * 3)
    h = make_harness(company, reviewer=reviewer)
    await h.new_ticket(labels=["low-risk"])
    await h.runner.run_until_idle()
    assert (await h.tickets.get("T-1")).status is S.ESCALATED

    async def merge(ticket_id: str, actor: str, comment: str):  # noqa: ANN202
        return await merge_escalated(
            h.tickets, h.approvals, h.gateway, ticket_id, actor=actor, comment=comment
        )

    app = create_api_app(ApiDeps(h.tickets, h.approvals, merge_escalated=merge))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a") as c:
        body = {"action": "merge", "actor": "owner", "comment": "đã tự kiểm"}
        resp = await c.post("/tickets/T-1/escalation", json=body)
        assert resp.status_code == 200, resp.text
        # Ticket đã xong: gọi lại bị từ chối, không merge lần hai.
        assert (await c.post("/tickets/T-1/escalation", json=body)).status_code == 409

    t = await h.tickets.get("T-1")
    assert t.status is S.DONE
    assert h.gitlab.project(PROJECT).mrs[0]["state"] == "merged"
    merges = [e for e in h.audit.entries if e.tool == "merge_mr"]
    assert [(e.role, e.decision) for e in merges] == [("merge_bot", "allow")]

    await h.new_ticket("T-2")  # chưa escalate, chưa có MR
    with pytest.raises(EscalationError):
        await merge_escalated(h.tickets, h.approvals, h.gateway, "T-2", actor="owner")


class FailingGit:
    """Client tool: `tool` lỗi `times` lần đầu (None = luôn lỗi), như GitHub lỗi tạm hay hỏng."""

    def __init__(self, inner: ToolClient, tool: str, times: int | None) -> None:
        self._inner = inner
        self._tool = tool
        self.left = times
        self.failures = 0

    async def call(self, server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool == self._tool and (self.left is None or self.left > 0):
            if self.left is not None:
                self.left -= 1
            self.failures += 1
            raise ToolError("GitHub 502: Bad Gateway")
        return await self._inner.call(server, tool, args)

    async def list_tools(self, server: str) -> list[ToolSpec]:
        return await self._inner.list_tools(server)


async def test_transient_push_error_is_retried(company: Company) -> None:
    wrapped: list[FailingGit] = []

    def wrap(c: ToolClient) -> ToolClient:
        wrapped.append(FailingGit(c, "push", times=1))
        return wrapped[-1]

    h = make_harness(company, wrap_client=wrap)
    await h.new_ticket(labels=["low-risk"])
    await h.runner.run_until_idle()
    assert wrapped[0].failures == 1
    assert (await h.tickets.get("T-1")).status is S.AWAITING_MERGE_APPROVAL
    assert len(h.gitlab.project(PROJECT).mrs) == 1


async def test_push_failure_escalates_and_keeps_changes(company: Company) -> None:
    h = make_harness(company, wrap_client=lambda c: FailingGit(c, "push", times=None))
    await h.new_ticket(labels=["low-risk"])
    await h.runner.run_until_idle()

    t = await h.tickets.get("T-1")
    assert t.status is S.ESCALATED  # không còn FAILED làm mất code
    event = next(e for e in await h.tickets.events("T-1") if e.to_status is S.ESCALATED)
    assert "đẩy code lên Git lỗi" in event.data["reason"] and "502" in event.data["reason"]
    kept = event.data["unpushed_changes"]
    assert kept and all(c["path"] and "content" in c for c in kept)
    assert not h.gitlab.project(PROJECT).mrs


async def test_escalation_keeps_last_review_notes(company: Company) -> None:
    reviewer = ScriptedReviewer([ReviewResult(False, f"vòng {i}: thiếu test") for i in (1, 2, 3)])
    h = make_harness(company, reviewer=reviewer)
    await h.new_ticket(labels=["low-risk"])
    await h.runner.run_until_idle()

    event = next(e for e in await h.tickets.events("T-1") if e.to_status is S.ESCALATED)
    assert event.data["review_notes"] == "vòng 3: thiếu test"
    assert event.data["review_rounds"] == 3
    assert "unpushed_changes" not in event.data


async def test_reviewer_gets_approved_plan(company: Company) -> None:
    seen: list[str] = []

    class Recording:
        async def review(
            self, ticket: Ticket, mr: dict[str, Any], plan: str = "", requirements: str = ""
        ) -> ReviewResult:
            seen.append(plan)
            return ReviewResult(True, "đạt")

    h = make_harness(company, reviewer_agent=lambda _: Recording())
    await h.new_ticket(labels=["low-risk"])
    await h.runner.run_until_idle()
    assert seen and seen[0].strip()


async def test_retry_push_then_merge_escalated_ticket(company: Company) -> None:
    import httpx

    from aico.api import ApiDeps, create_api_app
    from aico.orchestrator.escalation import EscalationError, merge_escalated, retry_push

    wrapped: list[FailingGit] = []

    def wrap(c: ToolClient) -> ToolClient:
        wrapped.append(FailingGit(c, "push", times=None))
        return wrapped[-1]

    h = make_harness(company, wrap_client=wrap)
    await h.new_ticket(labels=["low-risk"])
    await h.runner.run_until_idle()
    assert (await h.tickets.get("T-1")).status is S.ESCALATED

    async def retry(ticket_id: str, actor: str, comment: str) -> Ticket:
        return await retry_push(
            h.tickets, h.gateway, ticket_id, actor=actor, comment=comment, default_project=PROJECT
        )

    async def merge(ticket_id: str, actor: str, comment: str) -> Ticket:
        return await merge_escalated(
            h.tickets, h.approvals, h.gateway, ticket_id, actor=actor, comment=comment
        )

    app = create_api_app(ApiDeps(h.tickets, h.approvals, merge_escalated=merge, retry_push=retry))
    body = {"action": "retry_push", "actor": "owner", "comment": "GitHub đã ổn"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a") as c:
        # GitHub vẫn lỗi: báo lỗi, ticket giữ escalated và vẫn còn code để thử lại.
        assert (await c.post("/tickets/T-1/escalation", json=body)).status_code == 409
        wrapped[0].left = 0  # GitHub hết lỗi
        resp = await c.post("/tickets/T-1/escalation", json=body)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "escalated"  # người xem MR rồi mới quyết merge
        again = await c.post("/tickets/T-1/escalation", json=body)
        assert again.status_code == 200  # bấm lại không tạo MR hay commit trùng
        mrs = h.gitlab.project(PROJECT).mrs
        assert len(mrs) == 1 and mrs[0]["source_branch"] == "ai/T-1"
        merge_body = {"action": "merge", "actor": "owner", "comment": "đã xem"}
        assert (await c.post("/tickets/T-1/escalation", json=merge_body)).status_code == 200

    assert (await h.tickets.get("T-1")).status is S.DONE
    assert h.gitlab.project(PROJECT).mrs[0]["state"] == "merged"

    reviewer = ScriptedReviewer([ReviewResult(False, "x")] * 3)
    h2 = make_harness(company, reviewer=reviewer)
    await h2.new_ticket(labels=["low-risk"])
    await h2.runner.run_until_idle()
    with pytest.raises(EscalationError, match="không có code chờ đẩy lại"):
        await retry_push(h2.tickets, h2.gateway, "T-1", actor="owner", default_project=PROJECT)
