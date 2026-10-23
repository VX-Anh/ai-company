"""Tech Lead qua worker CLI: aico -> worker server (giả) -> tool bridge -> guard -> MCP server."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
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
    assert by_path["logo.png"] == {
        "path": "logo.png",
        "action": "create",
        "content": base64.b64encode(bytes(range(256))).decode(),
        "encoding": "base64",
    }
    assert skipped == []


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
    assert codex[codex.index("--sandbox") + 1] == "danger-full-access"
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
    assert set(by_name["claude"]["roles"]) == {
        "tech_lead",
        "coder",
        "reviewer",
        "designer",
        "researcher",
        "ba",
        "tester",
    }
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
    commits = [c["message"] for c in h.gitlab.project(PROJECT).commits["ai/T-1"]]
    assert commits[-2].startswith("T-1/W1: Thêm hướng dẫn cài đặt vào README")
    assert commits[-1].startswith("T-1: cập nhật tài liệu kế hoạch (lần 1)")
    assert "| W1 |" in files["docs/plans/T-1.md"]
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


def test_archive_diff_and_verdict_parsing() -> None:
    from aico.agents.cli_agent import archive_diff, parse_verdict

    base = _tar_gz({"a.ts": "1\n2\n", "gone.ts": "x\n", "same.ts": "s\n"})
    head = _tar_gz({"a.ts": "1\n3\n", "new.ts": "n\n", "same.ts": "s\n"}, root="other-root")
    diff = archive_diff(base, head)
    assert "-2\n+3\n" in diff and "+++ b/new.ts" in diff and "--- a/gone.ts" in diff
    assert "same.ts" not in diff

    assert parse_verdict("Ổn.\n\n**KẾT LUẬN: ĐẠT**") == (True, "Ổn.")
    assert parse_verdict("Thiếu test cho tên rỗng.\nKẾT LUẬN: CẦN SỬA") == (
        False,
        "Thiếu test cho tên rỗng.",
    )
    assert parse_verdict("không có kết luận") is None


def test_worker_review_mode_reads_and_runs_but_does_not_edit(tmp_path: Path) -> None:
    ws = _load_worker_server()
    job = {"prompt": "p", "mcp_url": "u", "mcp_token": "t", "mode": "review"}
    claude, _ = ws.build_command(job, "claude", tmp_path)
    allowed = claude[claude.index("--allowedTools") + 1]
    assert "Bash" in allowed and "Edit" not in allowed and "Write" not in allowed


async def test_cli_reviewer_requests_changes_then_passes(company: Company) -> None:
    """Reviewer (phòng codex) yêu cầu sửa -> Coder (phòng claude) sửa lại -> Reviewer đạt."""
    from aico.agents import CliCoder, CliReviewer

    calls: list[tuple[str, str]] = []
    verdicts = ["Biển tên đè lên chân bàn.\nKẾT LUẬN: CẦN SỬA", "Đã ổn.\nKẾT LUẬN: ĐẠT"]

    async def worker(request: httpx.Request) -> httpx.Response:
        job = json.loads(request.content)
        name = request.url.host
        if job["role"] == "coder" and name != "claude":
            return httpx.Response(409, json={"error": "đủ agent"})
        calls.append((name, job["role"]))
        if job["role"] == "coder":
            n = sum(1 for _, r in calls if r == "coder")
            change = {"path": "README.md", "action": "update", "content": f"# pilot v{n}\n"}
            return httpx.Response(
                200, json={"ok": True, "result": "TÓM TẮT: sửa", "changes": [change]}
            )
        assert job["mode"] == "review" and "<diff>" in job["prompt"]
        assert "+# pilot v" in job["prompt"]
        # Chấm theo kế hoạch đã duyệt: yêu cầu trái kế hoạch chỉ là gợi ý, không bắt sửa.
        assert "<plan>" in job["prompt"] and "Gợi ý ngoài kế hoạch" in job["prompt"]
        return httpx.Response(200, json={"ok": True, "result": verdicts.pop(0)})

    client = httpx.AsyncClient(transport=httpx.MockTransport(worker))
    runner = CliAgentRunner(
        company, TaskTokens(), CliSettings(WORKERS, "worker-secret", "u", 0.01, client)
    )
    h = make_harness(
        company,
        coder=lambda gw: CliCoder(runner, gw, default_project=PROJECT),
        reviewer_agent=lambda gw: CliReviewer(runner, gw),
    )
    h.gitlab.project(PROJECT).files["main"]["README.md"] = "# pilot\n"
    await h.new_ticket(labels=["small-fix"])
    await h.runner.run_until_idle()

    assert (await h.tickets.get("T-1")).status is TicketStatus.AWAITING_MERGE_APPROVAL
    assert calls == [
        ("claude", "coder"),
        ("codex", "reviewer"),
        ("claude", "coder"),
        ("codex", "reviewer"),
    ]
    assert h.gitlab.project(PROJECT).files["ai/T-1"]["README.md"] == "# pilot v2\n"

    # Ghi chú review hiện trên /events: góp ý khi quay lại Coder, kết luận khi chờ duyệt merge.
    events = await h.tickets.events("T-1")
    redo = [e for e in events if e.to_status is TicketStatus.IN_PROGRESS]
    assert redo[0].data == {"attempt": 1}
    assert redo[1].data["attempt"] == 2
    assert "[review bởi codex]" in redo[1].data["feedback"]
    assert "đè lên chân bàn" in redo[1].data["feedback"]
    merge = next(e for e in events if e.to_status is TicketStatus.AWAITING_MERGE_APPROVAL)
    assert merge.data["review_notes"] == "[review bởi codex]\nĐã ổn."


@pytest.mark.parametrize("shot_projects", [(PROJECT,), ("group/khac",)])
async def test_cli_reviewer_asks_gui_repo_to_take_screenshots(
    company: Company, shot_projects: tuple[str, ...]
) -> None:
    """Repo có giao diện (có bản xem trước): reviewer được nhắc chụp ảnh bằng aico-shot."""
    from aico.agents import SHOT_HINT, CliCoder, CliReviewer

    prompts: list[str] = []
    shots = [{"name": "1440x900.png", "data": "iVBO"}]
    sunk: list[tuple[str, list[dict[str, Any]]]] = []

    async def sink(ticket_id: str, items: list[dict[str, Any]]) -> None:
        sunk.append((ticket_id, items))
        raise RuntimeError("máy build lỗi")  # lỗi lưu ảnh không chặn review

    async def worker(request: httpx.Request) -> httpx.Response:
        job = json.loads(request.content)
        if job["role"] == "coder":
            change = {"path": "README.md", "action": "update", "content": "# pilot v1\n"}
            return httpx.Response(
                200, json={"ok": True, "result": "TÓM TẮT: sửa", "changes": [change]}
            )
        prompts.append(job["prompt"])
        result = {"ok": True, "result": "Đã xem ảnh.\nKẾT LUẬN: ĐẠT", "shots": shots}
        return httpx.Response(200, json=result)

    client = httpx.AsyncClient(transport=httpx.MockTransport(worker))
    runner = CliAgentRunner(
        company, TaskTokens(), CliSettings(WORKERS, "worker-secret", "u", 0.01, client)
    )
    h = make_harness(
        company,
        coder=lambda gw: CliCoder(runner, gw, default_project=PROJECT),
        reviewer_agent=lambda gw: CliReviewer(
            runner, gw, shot_hint=SHOT_HINT, shot_projects=shot_projects, shot_sink=sink
        ),
    )
    h.gitlab.project(PROJECT).files["main"]["README.md"] = "# pilot\n"
    await h.new_ticket(labels=["small-fix"])
    await h.runner.run_until_idle()

    assert len(prompts) == 1
    assert ("aico-shot" in prompts[0]) is (PROJECT in shot_projects)
    assert sunk == [("T-1", shots)]
    assert (await h.tickets.get("T-1")).status is TicketStatus.AWAITING_MERGE_APPROVAL


def test_worker_collects_review_screenshots(tmp_path: Path) -> None:
    ws = _load_worker_server()
    assert ws.collect_shots(tmp_path) == []
    shot_dir = tmp_path / ".aico-shots"
    shot_dir.mkdir()
    (shot_dir / "b.png").write_bytes(b"png-b")
    (shot_dir / "a.png").write_bytes(b"png-a")
    (shot_dir / "note.txt").write_text("x")
    (shot_dir / "big.png").write_bytes(b"x" * (ws.MAX_SHOT_BYTES + 1))
    shots = ws.collect_shots(tmp_path)
    assert [s["name"] for s in shots] == ["a.png", "b.png"]
    assert base64.b64decode(shots[0]["data"]) == b"png-a"


def test_run_agent_stops_stalled_agent(tmp_path: Path) -> None:
    import sys

    ws = _load_worker_server()
    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
    with pytest.raises(ws.AgentStalled, match="treo"):
        ws.run_agent(
            sleeper, tmp_path, dict(os.environ), timeout=20, idle_seconds=1, check_every=0.3
        )


def test_run_agent_keeps_agent_that_writes_files(tmp_path: Path) -> None:
    import sys

    ws = _load_worker_server()
    writer = [
        sys.executable,
        "-c",
        "import time\nfor i in range(8):\n    open(f'f{i}.txt','w').write('x'); time.sleep(0.25)\n"
        "print('xong')",
    ]
    code, out, _ = ws.run_agent(
        writer, tmp_path, dict(os.environ), timeout=20, idle_seconds=1, check_every=0.3
    )
    assert code == 0 and out.strip() == "xong"


@pytest.mark.parametrize("kind", ["claude", "codex", "cursor"])
def test_large_prompt_goes_through_stdin(kind: str, tmp_path: Path) -> None:
    """Prompt review có diff lớn (> 128 KB, giới hạn một tham số trên Linux) đi qua stdin."""
    import sys

    ws = _load_worker_server()
    prompt = "diff " + "x" * 300_000
    job = {"prompt": prompt, "system_prompt": "S", "mcp_url": "u", "mcp_token": "t"}
    cmd, _ = ws.build_command(job, kind, tmp_path, tmp_path)
    assert all(len(arg) < 10_000 for arg in cmd) and prompt not in cmd
    stdin = ws.prompt_input(job, kind)
    assert stdin.endswith(prompt)
    assert stdin.startswith("S\n\n") == (kind != "claude")  # Claude: --append-system-prompt
    counter = [sys.executable, "-c", "import sys; print(len(sys.stdin.read()))"]
    code, out, _ = ws.run_agent(
        counter, tmp_path, dict(os.environ), timeout=20, check_every=0.3, stdin=stdin
    )
    assert code == 0 and int(out) == len(stdin)


async def test_cors_allows_any_localhost_port(company: Company) -> None:
    from aico.api import ApiDeps, create_api_app
    from aico.settings import Settings

    h = make_harness(company)
    app = create_api_app(
        ApiDeps(
            h.tickets, h.approvals, api_token="api", cors_origin_regex=Settings.cors_origin_regex
        )
    )
    preflight = {
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a") as c:
        for origin, allowed in [
            ("http://localhost:5174", True),
            ("http://127.0.0.1:5199", True),
            ("http://evil.example", False),
            ("http://localhost.evil.example", False),
        ]:
            resp = await c.options("/workers", headers={"Origin": origin, **preflight})
            assert (resp.headers.get("access-control-allow-origin") == origin) is allowed, origin


CLAUDE_USAGE = """You are currently using your subscription to power your Claude Code usage

Current session: 28% used · resets Oct 20, 7:59pm (UTC)
Current week (all models): 14% used · resets Oct 23, 1:59pm (UTC)

What's contributing to your limits usage?
"""


def test_parse_claude_usage_windows() -> None:
    from datetime import UTC, datetime

    ws = _load_worker_server()
    now = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    session, week = ws.parse_claude_usage(CLAUDE_USAGE, now)
    assert session == {
        "name": "session",
        "window_minutes": 300,
        "used_percent": 28.0,
        "resets_at": "2026-10-20T19:59:00+00:00",
    }
    assert week["name"] == "week (all models)" and week["used_percent"] == 14.0
    assert week["resets_at"] == "2026-10-23T13:59:00+00:00"
    # Cuối năm: "Jan 2" nghĩa là năm sau.
    assert ws._claude_reset("Jan 2, 9am (UTC)", datetime(2026, 12, 30, tzinfo=UTC)).startswith(
        "2027-01-02T09:00"
    )


def test_parse_codex_rate_limits_and_limited_state() -> None:
    from datetime import UTC, datetime

    ws = _load_worker_server()
    result = {
        "rateLimits": {
            "primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": 1792528919},
            "secondary": {"usedPercent": 2, "windowDurationMins": 10080, "resetsAt": 1793082799},
            "planType": "plus",
        }
    }
    windows, plan = ws.parse_codex_rate_limits(result)
    assert plan == "plus"
    assert [(w["name"], w["used_percent"]) for w in windows] == [("5h", 100.0), ("week", 2.0)]

    usage = {"windows": windows}
    before = datetime.fromtimestamp(1792528919 - 60, UTC)
    after = datetime.fromtimestamp(1792528919 + 60, UTC)
    assert ws.usage_limited_until(usage, before) == windows[0]["resets_at"]
    assert ws.usage_limited_until(usage, after) is None

    stats = ws.WorkerStats(Path(os.devnull))
    snap = stats.snapshot("codex", 2, before, usage)
    assert snap["state"] == "limited" and snap["usage"] is usage
    assert snap["limit"]["resets_at"] == windows[0]["resets_at"]
    assert stats.snapshot("codex", 2, after, usage)["state"] == "idle"


def test_room_rank_by_usage() -> None:
    from aico.agents.cli_agent import room_rank

    def usage(*percents: float) -> dict[str, Any]:
        return {"state": "idle", "usage": {"windows": [{"used_percent": p} for p in percents]}}

    assert room_rank(usage(10, 30), 80) == 0
    assert room_rank(usage(10, 85), 80) == 1  # cửa sổ tuần sắp hết cũng tính
    assert room_rank({"state": "idle", "usage": None}, 80) == 0  # Cursor: không công bố
    assert room_rank({"state": "limited"}, 80) == 2
    assert room_rank({"state": "offline"}, 80) == 2


@pytest.mark.parametrize(("claude_used", "first"), [(90, "codex"), (20, "claude")])
async def test_dispatch_prefers_rooms_with_headroom(
    company: Company, claude_used: float, first: str
) -> None:
    runs: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.host
        if request.method == "GET":
            used = claude_used if name == "claude" else 10
            windows = [{"used_percent": used}]
            return httpx.Response(200, json={"state": "idle", "usage": {"windows": windows}})
        runs.append(name)
        return httpx.Response(200, json={"ok": True, "result": "xong"})

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
    ticket = Ticket(id="T-1", title="x", project=PROJECT)
    assert await runner.run("tech_lead", ticket, "lập kế hoạch") == "xong"
    assert runs == [first]


def test_worker_order_puts_slow_rooms_last_for_critical_work() -> None:
    from aico.agents.cli_agent import worker_order

    rotated = ("cursor", "claude", "codex")  # spread xoay Cursor lên đầu
    even = {"claude": 0, "codex": 0, "cursor": 0}
    assert worker_order(rotated, even) == rotated  # việc không găng: giữ thứ tự xoay
    assert worker_order(rotated, even, ("cursor",)) == ("claude", "codex", "cursor")
    # Hạn mức vẫn ưu tiên trước: Claude gần hết thì xuống sau cả Cursor.
    tight = {"claude": 1, "codex": 0, "cursor": 0}
    assert worker_order(rotated, tight, ("cursor",)) == ("codex", "cursor", "claude")


def test_blocking_items_are_the_ones_others_wait_for() -> None:
    from aico.agents.cli_agent import blocking_items
    from aico.orchestrator.workplan import WorkItem

    items = [
        WorkItem("W1", "token", "coder", ("a.css",)),
        WorkItem("W2", "hàm thuần", "coder", ("b.ts",)),
        WorkItem("W3", "thanh bên", "coder", ("c.ts",), depends_on=("W2",)),
        WorkItem("W6", "ghép", "coder", ("d.ts",), depends_on=("W1", "W3")),
    ]
    assert blocking_items(items) == {"W1", "W2", "W3"}


@pytest.mark.parametrize(("critical", "first"), [(True, "claude"), (False, "cursor")])
async def test_critical_job_skips_slow_room_first(
    company: Company, critical: bool, first: str
) -> None:
    runs: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"state": "idle", "usage": None})
        runs.append(request.url.host)
        return httpx.Response(200, json={"ok": True, "result": "xong"})

    runner = CliAgentRunner(
        company,
        TaskTokens(),
        CliSettings(
            WORKERS,
            "worker-secret",
            "http://aico:8300/mcp",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            slow_workers=("cursor",),
        ),
    )
    ticket = Ticket(id="T-1", title="x", project=PROJECT)
    # spread=2 xoay Cursor lên đầu (coder khai báo claude, codex, cursor).
    await runner.run_job("coder", ticket, "làm", mode="write", spread=2, critical=critical)
    assert runs == [first]
