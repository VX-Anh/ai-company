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
