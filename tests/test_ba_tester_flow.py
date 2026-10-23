"""Luồng có BA (phân tích yêu cầu trước Tech Lead) và Tester (kiểm thử sau review)."""

from __future__ import annotations

from dataclasses import dataclass, field

from aico.core import Ticket, TicketStatus
from aico.orchestrator import ReviewResult, ScriptedReviewer
from aico.registry import Company
from harness import PROJECT, make_harness

S = TicketStatus
REQS = "TC1: Cho ticket mới, Khi mở trang, Thì thấy tên ticket."


@dataclass
class FakeAnalyst:
    calls: list[str] = field(default_factory=list)

    async def analyze(self, ticket: Ticket) -> str:
        self.calls.append(ticket.id)
        return REQS


async def test_ba_requirements_reach_planner_reviewer_and_tester(company: Company) -> None:
    analyst, reviewer, tester = FakeAnalyst(), ScriptedReviewer(), ScriptedReviewer()
    h = make_harness(company, reviewer=reviewer, analyst=analyst, tester=tester)
    await h.new_ticket()

    await h.runner.run_until_idle()
    assert analyst.calls == ["T-1"]
    events = await h.tickets.events("T-1")
    planned = next(e for e in events if e.to_status is S.PLANNED)
    assert planned.data["requirements"] == REQS
    waiting = next(e for e in events if e.to_status is S.AWAITING_PLAN_APPROVAL)
    assert waiting.data["requirements"] == REQS
    assert "Theo yêu cầu: TC1" in waiting.data["plan"]

    await h.decide("T-1", "approved")
    await h.runner.run_until_idle()
    assert (await h.tickets.get("T-1")).status is S.AWAITING_MERGE_APPROVAL
    assert reviewer.requirements_seen == [REQS]
    assert tester.requirements_seen == [REQS]
    statuses = [e.to_status for e in await h.tickets.events("T-1") if e.kind == "status"]
    assert statuses[-3:] == [S.IN_REVIEW, S.IN_TESTING, S.AWAITING_MERGE_APPROVAL]
    merge = [e for e in await h.tickets.events("T-1") if e.to_status is S.AWAITING_MERGE_APPROVAL]
    assert merge[-1].data["test_notes"] == "đạt"


async def test_low_risk_and_retro_tickets_skip_ba(company: Company) -> None:
    analyst = FakeAnalyst()
    h = make_harness(company, analyst=analyst)
    await h.new_ticket("T-1", labels=["low-risk"])
    await h.new_ticket("T-2", labels=["retro"])
    await h.runner.run_until_idle()
    assert analyst.calls == []
    planned = next(e for e in await h.tickets.events("T-2") if e.to_status is S.PLANNED)
    assert "requirements" not in planned.data


async def test_tester_failure_goes_back_to_coder_with_notes(company: Company) -> None:
    tester = ScriptedReviewer([ReviewResult(False, "[kiểm thử bởi codex]\nTC1 không đạt")])
    h = make_harness(company, tester=tester)
    await h.new_ticket(labels=["low-risk"])
    await h.runner.run_until_idle()

    assert (await h.tickets.get("T-1")).status is S.AWAITING_MERGE_APPROVAL
    statuses = [e.to_status for e in await h.tickets.events("T-1") if e.kind == "status"]
    assert statuses[2:] == [
        S.IN_PROGRESS,
        S.IN_REVIEW,
        S.IN_TESTING,
        S.IN_PROGRESS,
        S.IN_REVIEW,
        S.IN_TESTING,
        S.AWAITING_MERGE_APPROVAL,
    ]
    redo = [e for e in await h.tickets.events("T-1") if e.to_status is S.IN_PROGRESS]
    assert "TC1 không đạt" in redo[1].data["feedback"]
    # Góp ý của Tester tới Coder (ScriptedCoder ghi feedback vào file).
    note = h.gitlab.project(PROJECT).files["ai/T-1"]["notes/T-1.md"]
    assert isinstance(note, str) and "TC1 không đạt" in note


async def test_tester_failures_are_capped_then_escalate(company: Company) -> None:
    rounds = company.sop("fix_ticket").limits.max_test_rounds
    tester = ScriptedReviewer([ReviewResult(False, "TC2 lỗi khi offline")] * rounds)
    h = make_harness(company, tester=tester)
    await h.new_ticket(labels=["low-risk"])
    await h.runner.run_until_idle()

    assert (await h.tickets.get("T-1")).status is S.ESCALATED
    escalated = next(e for e in await h.tickets.events("T-1") if e.to_status is S.ESCALATED)
    assert escalated.data["reason"] == f"kiểm thử chưa đạt sau {rounds} vòng"
    assert escalated.data["test_notes"] == "TC2 lỗi khi offline"
    assert escalated.data["test_rounds"] == rounds
