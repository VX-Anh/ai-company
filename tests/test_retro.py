"""Hồi cứu nội bộ: gom lịch sử ticket, chọn Researcher theo nhãn retro."""

from __future__ import annotations

from aico.agents import RETRO_LABEL, RetroPlanner
from aico.agents.retro import collect_history, render_history
from aico.core import Ticket, TicketStatus
from aico.orchestrator import LabelPlanner, StaticPlanner
from aico.tickets import InMemoryTicketStore

S = TicketStatus


async def _finished_ticket(store: InMemoryTicketStore, tid: str, *, labels: list[str]) -> None:
    await store.create(Ticket(id=tid, title=f"Việc {tid}", labels=labels), actor="api")
    for to in (S.TRIAGED, S.PLANNED):
        await store.transition(tid, to, actor="t")
    await store.transition(
        tid, S.AWAITING_PLAN_APPROVAL, actor="t", data={"work_items": [{"id": "W1"}, {"id": "W2"}]}
    )
    await store.set_pending_resume(
        tid, {"decision": "approved", "actor": "owner", "comment": "ok"}, actor="owner"
    )
    await store.transition(tid, S.IN_PROGRESS, actor="coder", data={"attempt": 1})
    await store.transition(tid, S.IN_REVIEW, actor="reviewer")
    await store.transition(
        tid,
        S.IN_PROGRESS,
        actor="coder",
        data={"attempt": 2, "feedback": "[review] quên kiểm rows rỗng"},
    )
    await store.transition(tid, S.IN_REVIEW, actor="reviewer")
    await store.transition(
        tid, S.AWAITING_MERGE_APPROVAL, actor="reviewer", data={"review_notes": "ĐẠT"}
    )
    await store.transition(tid, S.MERGING, actor="merge_bot")
    await store.transition(tid, S.DONE, actor="orchestrator")


async def test_history_summarizes_feedback_and_skips_retro_tickets() -> None:
    store = InMemoryTicketStore()
    await _finished_ticket(store, "T-1", labels=[])
    await _finished_ticket(store, "T-old-retro", labels=[RETRO_LABEL])
    await store.create(Ticket(id="T-open", title="đang chạy"), actor="api")

    history = await collect_history(store, exclude="T-retro")

    assert [h["id"] for h in history] == ["T-1"]
    h = history[0]
    assert h["code_attempts"] == 2
    assert h["work_items"] == 2
    assert h["feedback"] == ["[review] quên kiểm rows rỗng"]
    assert h["human_decisions"] == ["approved: ok"]
    text = render_history(history)
    assert "| T-1 | done |" in text
    assert "quên kiểm rows rỗng" in text


async def test_history_handles_old_events_and_failed_reason() -> None:
    store = InMemoryTicketStore()
    await store.create(Ticket(id="T-old", title="cũ"), actor="api")
    for to in (S.TRIAGED, S.PLANNED, S.IN_PROGRESS):
        await store.transition("T-old", to, actor="t")  # event cũ chưa có `attempt`
    await store.transition("T-old", S.FAILED, actor="runner", data={"error": "hết hạn mức"})

    [h] = await collect_history(store, exclude="x")
    assert h["code_attempts"] == 1
    assert h["escalation"] == "lỗi: hết hạn mức"


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def run(self, role_id: str, ticket: Ticket, instruction: str) -> str:
        self.calls.append((role_id, instruction))
        return "báo cáo"


async def test_retro_label_routes_planning_to_researcher_with_history() -> None:
    store = InMemoryTicketStore()
    await _finished_ticket(store, "T-1", labels=[])
    runner = _Recorder()
    planner = LabelPlanner(StaticPlanner(), {RETRO_LABEL: RetroPlanner(runner, store)})

    retro = Ticket(id="T-r", title="Hồi cứu", project="o/r", labels=["Retro"])
    assert await planner.plan(retro, None) == "báo cáo"
    role, prompt = runner.calls[0]
    assert role == "researcher"
    assert "<history>" in prompt and "quên kiểm rows rỗng" in prompt
    assert "Đề xuất cho aico" in prompt

    normal = await planner.plan(Ticket(id="T-2", title="Sửa nút"), None)
    assert normal.startswith("Kế hoạch cho T-2")
    assert len(runner.calls) == 1
