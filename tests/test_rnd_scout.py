"""Dò công nghệ định kỳ: watchlist của công ty dựng được ticket research hợp lệ."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

from aico.api import ApiDeps, create_api_app
from aico.governance import InMemoryApprovalStore
from aico.rnd.scout import WatchlistError, load_watchlist, scan_ticket
from aico.tickets import InMemoryTicketStore

ROOT = Path(__file__).resolve().parents[1]
KB = "VX-Anh/aico-knowledge"


def test_company_watchlist_builds_scan_ticket() -> None:
    wl = load_watchlist(ROOT / "company" / "rnd" / "watchlist.yaml")
    assert "aico" in wl.goals and ("anthropics/claude-code", "aico") in wl.release_repos
    ticket = scan_ticket(wl, today=date(2026, 11, 12), days=7, project=KB)
    assert ticket["project"] == KB and ticket["labels"][0] == "research"
    body = ticket["body"]
    assert "2026-11-05 → 2026-11-12" in body and "created_after=2026-11-05" in body
    assert "anthropics/claude-code [aico]" in body and "Báo cáo xu hướng" in body
    assert "giả thuyết" in body


def test_watchlist_rejects_unknown_goal(tmp_path: Path) -> None:
    path = tmp_path / "w.yaml"
    path.write_text(
        "goals: [{id: aico, text: x}]\nrelease_repos: [{repo: a/b, goal: game}]\n",
        encoding="utf-8",
    )
    with pytest.raises(WatchlistError, match="goal 'game'"):
        load_watchlist(path)
    path.write_text("release_repos: []\n", encoding="utf-8")
    with pytest.raises(WatchlistError, match="goal"):
        load_watchlist(path)


async def test_scan_ticket_endpoint() -> None:
    wl = load_watchlist(ROOT / "company" / "rnd" / "watchlist.yaml")
    deps = ApiDeps(
        tickets=InMemoryTicketStore(),
        approvals=InMemoryApprovalStore(),
        scan_ticket=lambda days: scan_ticket(wl, today=date(2026, 11, 12), days=days, project=KB),
    )
    app = create_api_app(deps)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        body = (await c.get("/rnd/scan-ticket", params={"days": 14})).json()
        assert body["title"] == "Dò công nghệ 2026-11-12 (14 ngày)"
        created = await c.post("/tickets", json=body)
        assert created.status_code == 201 and created.json()["labels"] == ["research", "scan"]


def _room(name: str = "claude", **extra: object) -> dict[str, object]:
    usage = {
        "windows": [
            {"window_minutes": 300, "used_percent": 20},
            {"window_minutes": 10080, "used_percent": 30},
        ]
    }
    return {"name": name, "state": "idle", "agents": [], "usage": usage, **extra}


def test_should_scan_conditions() -> None:
    from datetime import UTC, datetime, timedelta

    from aico.rnd.scheduler import ScanPolicy, should_scan

    now = datetime(2026, 11, 12, 2, tzinfo=UTC)
    on = ScanPolicy(interval_days=7)
    base = {"now": now, "policy": on, "last_scan_at": None, "active_tickets": 0}
    assert should_scan(**base, rooms=[_room()]) == (True, "tới kỳ, hệ rảnh")
    assert should_scan(**{**base, "policy": ScanPolicy()}, rooms=[_room()])[0] is False
    recent = {**base, "last_scan_at": now - timedelta(days=3)}
    assert should_scan(**recent, rooms=[_room()]) == (False, "chưa tới kỳ")
    assert should_scan(**{**base, "active_tickets": 1}, rooms=[_room()])[0] is False
    assert should_scan(**base, rooms=[_room(agents=[{"id": "a"}])])[0] is False
    hot = _room(usage={"windows": [{"window_minutes": 300, "used_percent": 55}]})
    assert should_scan(**base, rooms=[hot])[1] == "phòng claude đã dùng nhiều hạn mức"
    week = _room(usage={"windows": [{"window_minutes": 10080, "used_percent": 61}]})
    assert should_scan(**base, rooms=[week])[0] is False
    # Mọi phòng hết hạn mức hoặc tắt: không tạo ticket để rồi escalate.
    assert should_scan(**base, rooms=[_room(state="offline")]) == (
        False,
        "không phòng nào dùng được",
    )
    # Phòng hết hạn mức hoặc tắt không chặn phòng khác.
    assert should_scan(**base, rooms=[_room(), _room("cursor", state="limited", usage=None)])[0]


async def test_scheduler_creates_one_scan_ticket_per_interval() -> None:
    from datetime import UTC, datetime, timedelta

    from aico.rnd.scheduler import ScanPolicy, ScanScheduler

    store = InMemoryTicketStore()
    wl = load_watchlist(ROOT / "company" / "rnd" / "watchlist.yaml")
    clock = [datetime(2026, 11, 12, 2, tzinfo=UTC)]

    async def rooms() -> list[dict[str, object]]:
        return [_room()]

    sched = ScanScheduler(
        store,
        lambda days: scan_ticket(wl, today=clock[0].date(), days=days, project=KB),
        rooms,
        ScanPolicy(interval_days=7),
        lambda: clock[0],
    )
    first = await sched.tick()
    assert first is not None and "scan" in first.labels and first.project == KB
    # Ticket dò vừa tạo đang ở trạng thái new (đang chạy) và chưa tới kỳ: không tạo thêm.
    assert await sched.tick() is None
    assert len(await store.list()) == 1
    clock[0] += timedelta(days=8)
    assert await sched.tick() is None  # ticket dò trước vẫn chưa xong (new)
