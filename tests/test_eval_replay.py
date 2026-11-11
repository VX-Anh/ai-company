"""Ticket chạy lại (Phòng R&D, R1): nhánh eval/* từ commit gốc, MR vào eval/base/*, chỉ instance
eval nhận, không qua duyệt kế hoạch, không bao giờ merge vào main."""

from __future__ import annotations

from aico.core import Artifact, Ticket, TicketStatus
from aico.orchestrator.escalation import EscalationError, merge_escalated, retry_push
from aico.orchestrator.workplan import eval_spec, ticket_refs
from aico.registry import Company
from aico.rnd.metrics import collect
from harness import PROJECT, make_harness

S = TicketStatus


def eval_ticket(tid: str = "T-1", base: str = "main") -> Ticket:
    return Ticket(
        id=tid,
        title="Chạy lại: sửa nút",
        project=PROJECT,
        labels=["ui", "eval"],
        artifacts=[Artifact(kind="eval", ref=f"thu-prompt@{base}")],
    )


def test_ticket_refs_for_normal_and_eval_tickets() -> None:
    normal = Ticket(id="T-9", title="x", labels=["eval"])  # nhãn mà thiếu artifact: như thường
    assert ticket_refs(normal) == ticket_refs(normal.model_copy(update={"labels": []}))
    assert ticket_refs(normal).work == "ai/T-9" and ticket_refs(normal).target == "main"
    refs = ticket_refs(eval_ticket(base="a" * 40))
    assert (refs.work, refs.base, refs.target) == ("eval/T-1", "a" * 40, "eval/base/T-1")
    assert refs.eval_variant == "thu-prompt" and eval_spec(eval_ticket()) == ("thu-prompt", "main")


async def test_eval_ticket_runs_only_on_eval_instance_and_never_targets_main(
    company: Company,
) -> None:
    h = make_harness(company)
    await h.tickets.create(eval_ticket(), actor="eval")
    # aico chính không nhận ticket chạy lại.
    await h.runner.run_until_idle()
    assert (await h.tickets.get("T-1")).status is S.NEW

    e = make_harness(company, tickets=h.tickets, eval_only=True)
    e.gitlab.project(PROJECT).files["main"]["app.py"] = "print('cũ')\n"
    await e.runner.run_until_idle()
    t = await e.tickets.get("T-1")
    # Không qua duyệt kế hoạch (luật eval = low), tới chờ duyệt merge để evaluator chấm.
    assert t.status is S.AWAITING_MERGE_APPROVAL
    statuses = [ev.to_status for ev in await e.tickets.events("T-1") if ev.kind == "status"]
    assert S.AWAITING_PLAN_APPROVAL not in statuses
    files = e.gitlab.project(PROJECT).files
    assert "eval/T-1" in files and "eval/base/T-1" in files and "ai/T-1" not in files
    mrs = e.gitlab.project(PROJECT).mrs
    assert [(m["source_branch"], m["target_branch"]) for m in mrs] == [
        ("eval/T-1", "eval/base/T-1")
    ]
    # Ticket chạy lại không lẫn vào chỉ số của hệ thật.
    from datetime import timedelta

    since = t.created_at - timedelta(minutes=1)
    assert await collect(e.tickets, since=since) == []
    assert [r["id"] for r in await collect(e.tickets, since=since, eval_only=True)] == ["T-1"]


async def test_eval_ticket_cannot_merge_or_retry_push(company: Company) -> None:
    h = make_harness(company, eval_only=True)
    await h.tickets.create(eval_ticket(), actor="eval")
    await h.runner.run_until_idle()
    await h.decide("T-1", "rejected", "eval xong")
    await h.runner.run_until_idle()
    assert (await h.tickets.get("T-1")).status is S.ESCALATED
    import pytest

    with pytest.raises(EscalationError, match="không bao giờ merge"):
        await merge_escalated(h.tickets, h.approvals, h.gateway, "T-1", actor="owner")
    with pytest.raises(EscalationError, match="không đẩy lại"):
        await retry_push(h.tickets, h.gateway, "T-1", actor="owner", code_role="coder")
