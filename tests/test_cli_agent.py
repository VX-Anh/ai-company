"""Tech Lead qua worker CLI: aico -> worker server (giả) -> tool bridge -> guard -> MCP server."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from aico.agents import CliAgentRunner, CliSettings, LlmPlanner, WorkerUnavailable
from aico.core import Ticket, TicketStatus
from aico.llm_relay import TaskTokens
from aico.registry import Company
from aico.tools import ToolGateway
from aico.tools.bridge import ToolBridge, create_tool_bridge_app
from harness import PROJECT, make_harness

ROOT = Path(__file__).resolve().parents[1]
WORKERS = {
    "claude": "http://claude:8400",
    "codex": "http://codex:8400",
    "cursor": "http://cursor:8400",
}


def _load_worker_server() -> Any:
    spec = importlib.util.spec_from_file_location(
        "worker_server", ROOT / "deploy" / "worker" / "worker_server.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_workers(
    bridge: ToolBridge,
    jobs: list[tuple[str, dict[str, Any]]],
    status: dict[str, int],
    seen: list[dict[str, Any]] | None = None,
) -> httpx.AsyncClient:
    """Worker giả: dùng tool read_file qua bridge bằng token của task rồi trả kế hoạch."""

    async def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.host
        if request.headers["authorization"] != "Bearer worker-secret":
            return httpx.Response(401)
        if status.get(name, 200) != 200:
            return httpx.Response(status[name], json={"error": "busy"})
        job = json.loads(request.content)
        jobs.append((name, job))
        if seen is None:
            return httpx.Response(200, json={"ok": True, "result": "ok"})
        grant = bridge.grant(job["mcp_token"])
        assert grant is not None
        offered = {s.name for s in await bridge.list_tools(grant)}
        result = await bridge.call_tool(
            grant, "read_file", {"project": PROJECT, "path": "README.md"}
        )
        denied = await bridge.call_tool(grant, "push", {"project": PROJECT})
        seen.append(
            {
                "tools": offered,
                "readme": (result.structured_content or {}).get("content"),
                "push_denied": denied.is_error,
            }
        )
        return httpx.Response(200, json={"ok": True, "result": f"Kế hoạch từ {name}"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _factory(
    company: Company,
    tokens: TaskTokens,
    jobs: list[tuple[str, dict[str, Any]]],
    status: dict[str, int],
    seen: list[dict[str, Any]],
):  # type: ignore[no-untyped-def]
    def factory(gateway: ToolGateway) -> LlmPlanner:
        bridge = ToolBridge(gateway, tokens)
        runner = CliAgentRunner(
            company,
            tokens,
            CliSettings(
                workers=WORKERS,
                worker_token="worker-secret",
                bridge_url="http://aico:8300/mcp",
                busy_wait_seconds=0.01,
                http_client=_fake_workers(bridge, jobs, status, seen),
            ),
        )
        return LlmPlanner(runner)

    return factory


async def test_planner_runs_on_first_free_worker(company: Company) -> None:
    tokens, jobs, seen = TaskTokens(), [], []
    h = make_harness(company, planner=_factory(company, tokens, jobs, {"claude": 409}, seen))
    h.gitlab.project(PROJECT).files["main"]["README.md"] = "# pilot\n"
    await h.new_ticket(body="Sửa câu chữ README")

    await h.runner.run_until_idle()
    t = await h.tickets.get("T-1")
    assert t.status is TicketStatus.AWAITING_PLAN_APPROVAL

    # claude bận -> codex nhận; chỉ thấy tool tech_lead được cấp; push bị chặn.
    assert [name for name, _ in jobs] == ["codex"]
    job = jobs[0][1]
    assert job["mode"] == "read" and job["mcp_url"] == "http://aico:8300/mcp"
    assert "<ticket>" in job["prompt"] and job["system_prompt"]
    reads = [(e.role, e.tool, e.decision) for e in h.audit.entries]
    assert reads == [("tech_lead", "read_file", "allow")]
    assert seen == [{"tools": {"read_file"}, "readme": "# pilot\n", "push_denied": True}]
    assert tokens._grants == {}  # noqa: SLF001 - token tạm đã thu hồi


async def test_all_workers_failing_raises(company: Company) -> None:
    tokens: TaskTokens = TaskTokens()
    status = {"claude": 500, "codex": 500, "cursor": 500}
    h = make_harness(company)
    bridge = ToolBridge(h.gateway, tokens)
    runner = CliAgentRunner(
        company,
        tokens,
        CliSettings(
            WORKERS,
            "worker-secret",
            "http://aico:8300/mcp",
            0.01,
            _fake_workers(bridge, [], status),
        ),
    )
    ticket = Ticket(id="T-9", title="x", project=PROJECT)
    with pytest.raises(WorkerUnavailable, match="claude: HTTP 500"):
        await runner.run("tech_lead", ticket, "lập kế hoạch")
    assert tokens._grants == {}  # noqa: SLF001


async def test_bridge_rejects_missing_or_unknown_token(company: Company) -> None:
    h = make_harness(company)
    app = create_tool_bridge_app(ToolBridge(h.gateway, TaskTokens()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://b") as c:
        assert (await c.post("/mcp", json={})).status_code == 401
        resp = await c.post("/mcp", json={}, headers={"Authorization": "Bearer aico_nope"})
        assert resp.status_code == 401


@pytest.mark.parametrize("kind", ["claude", "codex", "cursor"])
def test_worker_command_read_mode_is_read_only(kind: str, tmp_path: Path) -> None:
    ws = _load_worker_server()
    job = {
        "prompt": "p",
        "system_prompt": "s",
        "mcp_url": "http://aico:8300/mcp",
        "mcp_token": "tk-123",
        "mode": "read",
    }
    cmd, env = ws.build_command(job, kind, tmp_path)
    joined = " ".join(cmd)
    if kind == "claude":
        assert "--tools=" in cmd and "dontAsk" in cmd and "--strict-mcp-config" in cmd
        assert "Bearer tk-123" in joined
    elif kind == "codex":
        assert cmd[cmd.index("--sandbox") + 1] == "read-only"
        assert env == {"AICO_TASK_TOKEN": "tk-123"} and "tk-123" not in joined
    else:
        assert cmd[cmd.index("--mode") + 1] == "ask"
        mcp = json.loads((tmp_path / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
        assert mcp["mcpServers"]["aico"]["headers"]["Authorization"] == "Bearer tk-123"


async def test_all_workers_failing_escalates_ticket(company: Company) -> None:
    tokens, jobs, seen = TaskTokens(), [], []
    status = {"claude": 500, "codex": 500, "cursor": 500}
    h = make_harness(company, planner=_factory(company, tokens, jobs, status, seen))
    await h.new_ticket()

    await h.runner.run_until_idle()
    t = await h.tickets.get("T-1")
    assert t.status is TicketStatus.ESCALATED
    reasons = [e.data.get("reason", "") for e in await h.tickets.events("T-1")]
    assert any("không worker nào làm được" in r for r in reasons)


def test_detect_limit_and_reset_time() -> None:
    from datetime import UTC, datetime

    ws = _load_worker_server()
    now = datetime(2026, 10, 20, 5, 22, tzinfo=UTC)
    claude = ws.detect_limit("You've hit your session limit · resets 6:30am (UTC)", now)
    assert claude["resets_at"] == "2026-10-20T06:30:00+00:00"
    tomorrow = ws.detect_limit("You've hit your session limit · resets 4am (UTC)", now)
    assert tomorrow["resets_at"] == "2026-10-21T04:00:00+00:00"
    codex = ws.detect_limit("You've hit your usage limit. Try again in 2 hours 5 minutes.", now)
    assert codex["resets_at"] == "2026-10-20T07:27:00+00:00"
    unknown = ws.detect_limit("Rate limit exceeded", now)
    assert unknown is not None and unknown["resets_at"] is None
    assert ws.detect_limit("file not found", now) is None


def test_worker_stats_track_room_agents_tokens_and_limit(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    ws = _load_worker_server()
    path = tmp_path / "stats.json"
    stats = ws.WorkerStats(path)
    now = datetime(2026, 10, 20, 5, 0, tzinfo=UTC)
    a = stats.start({"ticket_id": "T-1", "role": "tech_lead"}, now)
    b = stats.start({"ticket_id": "T-2", "role": "coder", "mode": "write"}, now)
    snap = stats.snapshot("claude", 2, now)
    assert snap["state"] == "working" and snap["slots"] == 2
    assert [(x["role"], x["ticket_id"]) for x in snap["agents"]] == [
        ("tech_lead", "T-1"),
        ("coder", "T-2"),
    ]
    stats.finish(a, {"ok": True, "duration_seconds": 3}, (100, 20), now)
    limit_error = {"ok": False, "error": "You've hit your session limit · resets 6:30am (UTC)"}
    stats.finish(b, limit_error, (0, 0), now)

    snap = stats.snapshot("claude", 2, now)
    assert snap["state"] == "limited" and snap["agents"] == [] and snap["current"] is None
    assert (snap["runs_ok"], snap["runs_failed"], snap["tokens_today"]) == (1, 1, 120)
    assert stats.limited_until(now + timedelta(hours=2)) is None  # qua giờ mở lại
    assert ws.WorkerStats(path).data["input_tokens"] == 100  # giữ qua restart
    assert ws.token_usage("codex", {}, "codex\nOK\ntokens used\n1,902\n") == (1902, 0)
    assert ws.token_usage("cursor", {"inputTokens": 5, "outputTokens": 2}, "") == (5, 2)


def _tar_gz(files: dict[str, str], root: str = "acme-pilot-abc123") -> str:
    import base64
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


def test_workspace_changes_follow_gitignore(tmp_path: Path) -> None:
    ws = _load_worker_server()
    repo = tmp_path / "repo"
    files = {".gitignore": "node_modules/\n", "a.ts": "1\n", "old.ts": "x\n", "src/b.ts": "2\n"}
    ws.extract_workspace(_tar_gz(files), repo)
    ws.snapshot_repo(repo)
    (repo / "a.ts").write_text("1\n2\n", encoding="utf-8", newline="\n")
    (repo / "old.ts").unlink()
    (repo / "src" / "new.ts").write_text("mới\n", encoding="utf-8", newline="\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "x.js").write_text("ignored", encoding="utf-8")
    (repo / ".cursor").mkdir()
    (repo / ".cursor" / "mcp.json").write_text("{}", encoding="utf-8")
    (repo / "logo.png").write_bytes(bytes(range(256)))

    changes, skipped = ws.collect_changes(repo)
    by_path = {c["path"]: c for c in changes}
    assert by_path["a.ts"] == {"path": "a.ts", "action": "update", "content": "1\n2\n"}
    assert by_path["src/new.ts"]["action"] == "create"
    assert by_path["old.ts"] == {"path": "old.ts", "action": "delete"}
    assert not any(p.startswith(("node_modules", ".cursor")) for p in by_path)
    assert skipped == ["logo.png (file nhị phân)"]


def test_workspace_rejects_path_escape(tmp_path: Path) -> None:
    ws = _load_worker_server()
    with pytest.raises(ValueError, match="không hợp lệ"):
        ws.extract_workspace(_tar_gz({"../../evil.sh": "x"}), tmp_path / "repo")


def test_worker_command_write_mode(tmp_path: Path) -> None:
    ws = _load_worker_server()
    job = {"prompt": "p", "mcp_url": "u", "mcp_token": "tk-123", "mode": "write"}
    claude, _ = ws.build_command(job, "claude", tmp_path)
    assert "--tools=" not in claude and "Bash" in claude[claude.index("--allowedTools") + 1]
    codex, _ = ws.build_command(job, "codex", tmp_path)
    assert codex[codex.index("--sandbox") + 1] == "workspace-write"
    repo = tmp_path / "repo"
    repo.mkdir()
    cursor, _ = ws.build_command(job, "cursor", tmp_path, repo)
    assert "--force" in cursor and "--mode" not in cursor
    assert (repo / ".cursor" / "mcp.json").exists()


async def test_workers_endpoint_reports_status_and_roles(company: Company) -> None:
    from aico.api import ApiDeps, create_api_app

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cursor":
            return httpx.Response(500)
        assert request.headers["authorization"] == "Bearer worker-secret"
        return httpx.Response(200, json={"kind": request.url.host, "state": "idle", "runs_ok": 2})

    runner = CliAgentRunner(
        company,
        TaskTokens(),
        CliSettings(
            WORKERS,
            "worker-secret",
            "http://aico:8300/mcp",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ),
    )
    h = make_harness(company)
    app = create_api_app(ApiDeps(h.tickets, h.approvals, api_token="api", workers=runner.statuses))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a") as c:
        assert (await c.get("/workers")).status_code == 401
        body = (await c.get("/workers", headers={"Authorization": "Bearer api"})).json()
    by_name = {w["name"]: w for w in body}
    assert by_name["claude"]["state"] == "idle" and by_name["claude"]["runs_ok"] == 2
    assert set(by_name["claude"]["roles"]) == {"tech_lead", "coder", "designer"}
    assert by_name["cursor"]["state"] == "offline"


def test_tool_images_pass_through_bridge_but_not_audit() -> None:
    import mcp_types as types

    from aico.tools.bridge import _text
    from aico.tools.gateway import IMAGES_KEY, _result_to_dict, _summary, without_images

    result = types.CallToolResult(
        content=[
            types.TextContent(type="text", text='{"ok": true, "revision": "r1"}'),
            types.ImageContent(type="image", data="iVBORw0KGgo=", mime_type="image/png"),
        ],
    )
    data = _result_to_dict(result)
    assert data["ok"] and data[IMAGES_KEY] == [{"mime_type": "image/png", "data": "iVBORw0KGgo="}]
    assert "iVBOR" not in _summary(data) and '"images": 1' in _summary(data)
    assert without_images(data) == {"ok": True, "revision": "r1", "images": 1}

    out = _text(data)
    assert [c.type for c in out.content] == ["text", "image"]
    assert out.structured_content == {"ok": True, "revision": "r1"}


async def test_cli_coder_edits_workspace_and_opens_mr(company: Company) -> None:
    """Coder qua worker: tải mã nguồn qua guard, agent sửa, aico push + mở MR qua guard."""
    import base64
    import io
    import tarfile

    from aico.agents import CliCoder

    jobs: list[dict[str, Any]] = []

    async def worker(request: httpx.Request) -> httpx.Response:
        job = json.loads(request.content)
        jobs.append(job)
        archive = base64.b64decode(job["workspace"]["data"])
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            names = [m.name.split("/", 1)[1] for m in tar.getmembers()]
        assert "README.md" in names
        changes = [
            {"path": "README.md", "action": "update", "content": "# pilot\n\n## Cài đặt\n"},
            {"path": "docs/setup.md", "action": "create", "content": "npm ci\n"},
        ]
        result = "Đã thêm mục cài đặt.\nTÓM TẮT: Thêm hướng dẫn cài đặt vào README"
        return httpx.Response(200, json={"ok": True, "result": result, "changes": changes})

    def coder_factory(gateway: ToolGateway) -> CliCoder:
        runner = CliAgentRunner(
            company,
            TaskTokens(),
            CliSettings(
                WORKERS,
                "worker-secret",
                "http://aico:8300/mcp",
                0.01,
                httpx.AsyncClient(transport=httpx.MockTransport(worker)),
            ),
        )
        return CliCoder(runner, gateway, default_project=PROJECT)

    h = make_harness(company, coder=coder_factory)
    h.gitlab.project(PROJECT).files["main"]["README.md"] = "# pilot\n"
    await h.new_ticket(body="Thêm hướng dẫn cài đặt")
    await h.runner.run_until_idle()
    await h.decide("T-1", "approved")
    await h.runner.run_until_idle()

    assert (await h.tickets.get("T-1")).status is TicketStatus.AWAITING_MERGE_APPROVAL
    assert jobs[0]["mode"] == "write" and jobs[0]["role"] == "coder"
    assert jobs[0]["workspace"]["ref"] == "main"  # chưa có branch ai/T-1
    files = h.gitlab.project(PROJECT).files["ai/T-1"]
    assert files["README.md"].endswith("## Cài đặt\n") and files["docs/setup.md"] == "npm ci\n"
    commit = h.gitlab.project(PROJECT).commits["ai/T-1"][-1]["message"]
    assert commit.startswith("T-1: Thêm hướng dẫn cài đặt vào README")
    tools = [(e.role, e.tool, e.decision) for e in h.audit.entries if e.role == "coder"]
    assert ("coder", "download_archive", "allow") in tools
    assert ("coder", "open_merge_request", "allow") in tools


async def test_cli_coder_without_changes_escalates(company: Company) -> None:
    from aico.agents import CliCoder

    async def worker(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": "không cần sửa", "changes": []})

    def coder_factory(gateway: ToolGateway) -> CliCoder:
        client = httpx.AsyncClient(transport=httpx.MockTransport(worker))
        runner = CliAgentRunner(
            company, TaskTokens(), CliSettings(WORKERS, "worker-secret", "u", 0.01, client)
        )
        return CliCoder(runner, gateway, default_project=PROJECT)

    h = make_harness(company, coder=coder_factory)
    h.gitlab.project(PROJECT).files["main"]["README.md"] = "# pilot\n"
    await h.new_ticket(labels=["small-fix"])  # rủi ro thấp: bỏ qua duyệt kế hoạch
    await h.runner.run_until_idle()
    t = await h.tickets.get("T-1")
    assert t.status is TicketStatus.ESCALATED
    reasons = [e.data.get("reason", "") for e in await h.tickets.events("T-1")]
    assert any("không sửa file nào" in r for r in reasons)
    assert "ai/T-1" not in h.gitlab.project(PROJECT).files  # không tạo branch rỗng
