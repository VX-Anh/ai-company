from __future__ import annotations

import httpx
import pytest

from aico.api import ApiDeps, create_api_app
from aico.core import TicketStatus
from aico.intake import GitLabIssuePoller
from aico.registry import Company
from harness import PROJECT, make_harness

S = TicketStatus


def _client(h: object, **kw: object) -> httpx.AsyncClient:
    deps = ApiDeps(tickets=h.tickets, approvals=h.approvals, default_project=PROJECT, **kw)  # type: ignore[attr-defined, arg-type]
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_api_app(deps)), base_url="http://api"
    )


async def test_ticket_lifecycle_over_http(company: Company) -> None:
    h = make_harness(company)
    async with _client(h) as api:
        created = await api.post("/tickets", json={"title": "Sửa lỗi", "labels": ["low-risk"]})
        assert created.status_code == 201
        tid = created.json()["id"]
        assert created.json()["project"] == PROJECT

        early = await api.post(
            f"/tickets/{tid}/decisions", json={"decision": "approved", "actor": "owner"}
        )
        assert early.status_code == 409  # chưa tới bước duyệt

        await h.runner.run_until_idle()
        assert (await api.get(f"/tickets/{tid}")).json()["status"] == "awaiting_merge_approval"

        bad = await api.post(
            f"/tickets/{tid}/decisions", json={"decision": "revise", "actor": "owner"}
        )
        assert bad.status_code == 422  # revise chỉ dành cho kế hoạch

        ok = await api.post(
            f"/tickets/{tid}/decisions", json={"decision": "approved", "actor": "owner"}
        )
        assert ok.status_code == 202
        dup = await api.post(
            f"/tickets/{tid}/decisions", json={"decision": "rejected", "actor": "owner"}
        )
        assert dup.status_code == 409

        await h.runner.run_until_idle()
        assert (await api.get(f"/tickets/{tid}")).json()["status"] == "done"
        events = (await api.get(f"/tickets/{tid}/events")).json()
        assert events[0]["kind"] == "created"


async def test_escalated_ticket_can_be_closed(company: Company) -> None:
    h = make_harness(company)
    await h.new_ticket(labels=["low-risk"])
    await h.runner.run_until_idle()
    await h.decide("T-1", "rejected")
    await h.runner.run_until_idle()
    async with _client(h) as api:
        resp = await api.post("/tickets/T-1/escalation", json={"action": "close", "actor": "owner"})
        assert resp.status_code == 200 and resp.json()["status"] == "done"


async def test_tokens_protect_endpoints(company: Company) -> None:
    h = make_harness(company)
    await h.new_ticket(labels=["low-risk"])
    await h.runner.run_until_idle()
    async with _client(h, api_token="user-secret", internal_token="mcp-secret") as api:
        assert (await api.get("/tickets")).status_code == 401
        assert (
            await api.get("/tickets", headers={"Authorization": "Bearer user-secret"})
        ).status_code == 200
        params = {"project": PROJECT, "mr_iid": 1}
        assert (
            await api.get(
                "/internal/approvals/A-x",
                params=params,
                headers={"Authorization": "Bearer user-secret"},
            )
        ).status_code == 401
        resp = await api.get(
            "/internal/approvals/A-x", params=params, headers={"Authorization": "Bearer mcp-secret"}
        )
        assert resp.json() == {"valid": False, "reason": "không có bản ghi duyệt"}


async def test_internal_endpoint_disabled_without_token(company: Company) -> None:
    h = make_harness(company)
    async with _client(h) as api:
        resp = await api.get("/internal/approvals/A-x", params={"project": PROJECT, "mr_iid": 1})
        assert resp.status_code == 401


async def test_intake_creates_each_issue_once(company: Company) -> None:
    h = make_harness(company)
    h.gitlab.project(PROJECT).issues.extend(
        [
            {
                "iid": 3,
                "title": "Nút lưu không chạy",
                "description": "...",
                "labels": ["ai-company", "low-risk"],
            },
            {"iid": 4, "title": "Không gắn nhãn", "labels": ["bug"]},
        ]
    )
    poller = GitLabIssuePoller(h.gateway, h.tickets, [PROJECT])
    created = await poller.poll_once()
    assert [t.source_ref for t in created] == [f"gitlab:{PROJECT}#3"]
    assert created[0].labels == ["low-risk"]
    assert await poller.poll_once() == []
    assert len(await h.tickets.list()) == 1


async def test_intake_cannot_read_other_projects(company: Company) -> None:
    from aico.tools import ToolDenied

    h = make_harness(company)
    with pytest.raises(ToolDenied):
        await GitLabIssuePoller(h.gateway, h.tickets, ["other/repo"]).poll_once()
