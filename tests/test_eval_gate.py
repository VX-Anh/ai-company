"""Cổng hạn mức của instance chạy lại: chỉ nhận ticket mới khi hệ thật rảnh, các phòng còn dư hạn
mức và chưa chạm trần 7 ngày; ticket đã bắt đầu vẫn chạy tiếp."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aico.core import Ticket, TicketStatus, utcnow
from aico.registry import Company
from aico.rnd import evalrun
from aico.rnd.evalgate import EvalGate, EvalPolicy, should_start
from aico.settings import Settings
from aico.tickets import InMemoryTicketStore
from harness import PROJECT, make_harness
from test_eval_replay import eval_ticket

S = TicketStatus


def _room(name: str = "claude", **extra: Any) -> dict[str, Any]:
    usage = {
        "windows": [
            {"window_minutes": 300, "used_percent": 20},
            {"window_minutes": 10080, "used_percent": 30},
        ]
    }
    return {"name": name, "state": "idle", "agents": [], "usage": usage, **extra}


def test_should_start_conditions() -> None:
    base: dict[str, Any] = {
        "policy": EvalPolicy(weekly_cap=3),
        "pending_new": 2,
        "started_last_week": 0,
        "active_tickets": 0,
    }
    assert should_start(**base, rooms=[_room()]) == (True, "hệ rảnh, còn hạn mức")
    assert should_start(**{**base, "pending_new": 0}, rooms=[_room()])[1] == "không có ticket mới"
    capped = should_start(**{**base, "started_last_week": 3}, rooms=[_room()])
    assert capped == (False, "đã chạy 3/3 ticket trong 7 ngày")
    assert should_start(**{**base, "started_last_week": 2}, rooms=[_room()])[0] is True
    # Trần 0: không chạy ticket mới nào.
    zero = {**base, "policy": EvalPolicy(weekly_cap=0)}
    assert should_start(**zero, rooms=[_room()])[0] is False
    busy = should_start(**{**base, "active_tickets": 1}, rooms=[_room()])
    assert busy == (False, "hệ thật đang có 1 ticket chạy")
    assert should_start(**base, rooms=[_room(agents=[{"id": "a"}])])[0] is False
    # Ngưỡng của eval: 70% cửa sổ 5 giờ, 60% tuần (thiết kế mục 5).
    ok5h = _room(usage={"windows": [{"window_minutes": 300, "used_percent": 69}]})
    hot5h = _room(usage={"windows": [{"window_minutes": 300, "used_percent": 70}]})
    week = _room(usage={"windows": [{"window_minutes": 10080, "used_percent": 60}]})
    assert should_start(**base, rooms=[ok5h])[0] is True
    assert should_start(**base, rooms=[hot5h])[1] == "phòng claude đã dùng nhiều hạn mức"
    assert should_start(**base, rooms=[week])[0] is False
    # Một phòng vượt ngưỡng là dừng, dù phòng khác còn dư (hạn mức để dành cho ticket thật).
    assert should_start(**base, rooms=[_room("codex"), hot5h])[0] is False
    assert should_start(**base, rooms=[_room(state="offline")])[1] == "không phòng nào dùng được"
    assert should_start(**base, rooms=[])[0] is False
    # Không có worker CLI (chạy thử với agent giả): chỉ còn trần tuần và hệ rảnh.
    assert should_start(**base, rooms=None)[0] is True


def test_settings_read_eval_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AICO_DATABASE_URL", "postgresql://x/y")
    for name in ("AICO_EVAL_WEEKLY_CAP", "AICO_EVAL_MAX_WEEK_PERCENT"):
        monkeypatch.delenv(name, raising=False)
    assert Settings.from_env().eval_weekly_cap == 40
    monkeypatch.setenv("AICO_EVAL_WEEKLY_CAP", "12")
    monkeypatch.setenv("AICO_EVAL_MAX_WEEK_PERCENT", "45")
    s = Settings.from_env()
    assert (s.eval_weekly_cap, s.eval_max_week_percent, s.eval_max_session_percent) == (12, 45, 70)
    monkeypatch.setenv("AICO_EVAL_WEEKLY_CAP", "0")
    assert Settings.from_env().eval_weekly_cap == 0


async def test_claim_skip_new_on_both_stores(store) -> None:  # type: ignore[no-untyped-def]
    await store.create(eval_ticket("T-new"), actor="eval")
    await store.create(eval_ticket("T-run"), actor="eval")
    await store.transition("T-run", S.TRIAGED, actor="t")
    kw: dict[str, Any] = {"lease_seconds": 30, "eval_only": True}
    got = await store.claim(owner="a", skip_new=True, **kw)
    assert got is not None and got.id == "T-run"  # ticket đã bắt đầu vẫn được nhận
    assert await store.claim(owner="b", skip_new=True, **kw) is None
    fresh = await store.claim(owner="c", **kw)
    assert fresh is not None and fresh.id == "T-new"


async def test_gate_counts_week_and_waits_for_real_tickets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = InMemoryTicketStore()
    now = [utcnow()]
    rooms: list[dict[str, Any]] = [_room()]
    calls = 0

    async def read_rooms() -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        return rooms

    gate = EvalGate(
        store, read_rooms, EvalPolicy(weekly_cap=2), lambda: now[0], variant="thu-prompt"
    )
    caplog.set_level(logging.INFO, logger="aico.rnd.evalgate")
    assert await gate() is False and gate.last_reason == "không có ticket mới"
    assert calls == 0  # không có ticket mới thì không hỏi các phòng

    await store.create(eval_ticket("T-1"), actor="eval")
    assert await gate() is False  # kết quả "chưa" giữ 60 giây
    now[0] += timedelta(seconds=61)
    assert await gate() is True and calls == 1

    # Ticket thật đang chạy (hoặc chờ runner xử lý quyết định): eval nhường.
    await store.create(Ticket(id="T-real", title="thật", project=PROJECT), actor="api")
    assert await gate() is False and gate.last_reason == "hệ thật đang có 1 ticket chạy"
    await store.transition("T-real", S.TRIAGED, actor="t")
    await store.transition("T-real", S.PLANNED, actor="t")
    await store.transition("T-real", S.AWAITING_PLAN_APPROVAL, actor="t")
    now[0] += timedelta(seconds=61)
    assert await gate() is True  # chờ người duyệt không tính là đang chạy
    await store.set_pending_resume("T-real", {"decision": "approved"}, actor="owner")
    assert await gate() is False
    assert await store.claim(owner="x", lease_seconds=30) is not None
    await store.take_pending_resume("T-real", owner="x")
    await store.transition("T-real", S.FAILED, actor="x")

    # Biến thể khác có ticket mới: không phải việc của instance này.
    now[0] += timedelta(seconds=61)
    await store.transition("T-1", S.TRIAGED, actor="t")
    other = eval_ticket("T-o").model_copy(
        update={"artifacts": [eval_ticket().artifacts[0].model_copy(update={"ref": "khac@main"})]}
    )
    await store.create(other, actor="eval")
    assert await gate() is False and gate.last_reason == "không có ticket mới"

    # Trần 7 ngày tính mọi biến thể, theo lúc ticket rời trạng thái new.
    now[0] += timedelta(seconds=61)
    await store.transition("T-o", S.TRIAGED, actor="t")
    await store.create(eval_ticket("T-2"), actor="eval")
    assert await gate() is False and gate.last_reason == "đã chạy 2/2 ticket trong 7 ngày"
    now[0] += timedelta(days=7, seconds=1)
    assert await gate() is True  # hai ticket kia đã ra khỏi cửa sổ 7 ngày

    rooms[0] = _room(agents=[{"id": "a"}])
    assert await gate() is False and gate.last_reason == "phòng claude đang có agent"
    reasons = [r.getMessage() for r in caplog.records if "chưa nhận" in r.getMessage()]
    assert any("đã chạy 2/2" in r for r in reasons) and any("đang có agent" in r for r in reasons)

    async def broken() -> list[dict[str, Any]]:
        raise RuntimeError("worker không trả lời")

    failing = EvalGate(store, broken, EvalPolicy(), lambda: now[0])
    assert await failing() is False and failing.last_reason == "kiểm tra lỗi: RuntimeError"


async def test_runner_with_closed_gate_still_finishes_started_tickets(company: Company) -> None:
    open_gate = [True]

    async def gate() -> bool:
        return open_gate[0]

    h = make_harness(company, eval_only=True, new_ticket_gate=gate)
    h.gitlab.project(PROJECT).files["main"]["app.py"] = "x\n"
    await h.tickets.create(eval_ticket("T-1"), actor="eval")
    await h.runner.run_until_idle()
    assert (await h.tickets.get("T-1")).status is S.AWAITING_MERGE_APPROVAL

    open_gate[0] = False
    await h.tickets.create(eval_ticket("T-2"), actor="eval")
    await evalrun.report(h.tickets, "thu-prompt", close=True)
    await h.runner.run_until_idle()
    # Cổng đóng: ticket mới nằm chờ, ticket đã chạy vẫn được đóng (không bỏ dở).
    assert (await h.tickets.get("T-2")).status is S.NEW
    assert (await h.tickets.get("T-1")).status is S.ESCALATED
    open_gate[0] = True
    await h.runner.run_until_idle()
    assert (await h.tickets.get("T-2")).status is S.AWAITING_MERGE_APPROVAL


async def test_gate_window_uses_start_time_not_creation() -> None:
    """Ticket tạo từ lâu nhưng mới bắt đầu hôm nay vẫn tính vào trần tuần này."""
    store = InMemoryTicketStore()
    old = datetime.now(UTC) - timedelta(days=30)
    await store.create(eval_ticket("T-1").model_copy(update={"created_at": old}), actor="eval")
    await store.transition("T-1", S.TRIAGED, actor="t")
    await store.create(eval_ticket("T-2"), actor="eval")
    gate = EvalGate(store, None, EvalPolicy(weekly_cap=1), utcnow)
    assert await gate.check() == (False, "đã chạy 1/1 ticket trong 7 ngày")
