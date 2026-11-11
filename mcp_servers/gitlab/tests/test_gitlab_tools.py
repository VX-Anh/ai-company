from __future__ import annotations

import base64

import pytest
from mcp.client.client import Client

from mcp_gitlab.backend import FakeGitLabBackend, GitLabError
from mcp_gitlab.server import build_server
from mcp_gitlab.tools import ConstraintError, Constraints, GitLabTools

PROJECT = "group/pilot-repo"


def _tools(verify_ok: bool | None = True) -> tuple[GitLabTools, FakeGitLabBackend]:
    backend = FakeGitLabBackend()
    backend.project(PROJECT).files["main"]["app.py"] = "print('hi')\n"

    async def verify(approval_id: str, project: str, mr_iid: int) -> tuple[bool, str]:
        return bool(verify_ok), "ok" if verify_ok else "chưa duyệt"

    tools = GitLabTools(
        backend, Constraints(allowed_projects=(PROJECT,)), verify if verify_ok is not None else None
    )
    return tools, backend


async def test_project_and_branch_constraints() -> None:
    tools, _ = _tools()
    with pytest.raises(ConstraintError):
        await tools.read_file("other/repo", "app.py")
    with pytest.raises(ConstraintError):
        await tools.create_branch(PROJECT, "main")
    with pytest.raises(ConstraintError):
        await tools.create_branch(PROJECT, "ai/../main")
    with pytest.raises(ConstraintError):
        await tools.open_merge_request(PROJECT, "ai/fix", "x", target_branch="release")


async def test_push_and_open_mr_are_idempotent() -> None:
    tools, backend = _tools()
    assert (await tools.create_branch(PROJECT, "ai/T-1"))["created"]
    assert not (await tools.create_branch(PROJECT, "ai/T-1"))["created"]
    files = [{"path": "app.py", "content": "print('fixed')\n"}]
    first = await tools.push(PROJECT, "ai/T-1", "Sửa lỗi", files, "T-1:code:1")
    second = await tools.push(PROJECT, "ai/T-1", "Sửa lỗi", files, "T-1:code:1")
    assert first["created"] and not second["created"]
    assert first["commit"] == second["commit"]
    assert backend.calls.count("commit") == 1
    mr1 = await tools.open_merge_request(PROJECT, "ai/T-1", "T-1: Sửa lỗi")
    mr2 = await tools.open_merge_request(PROJECT, "ai/T-1", "T-1: Sửa lỗi")
    assert mr1["created"] and not mr2["created"] and mr1["iid"] == mr2["iid"]


async def test_push_binary_files_as_base64() -> None:
    tools, backend = _tools()
    await tools.create_branch(PROJECT, "ai/T-3")
    png = base64.b64encode(bytes([0x89, 0x50, 0xFF])).decode()
    files = [{"path": "logo.png", "action": "create", "content": png, "encoding": "base64"}]
    await tools.push(PROJECT, "ai/T-3", "Thêm logo", files, "T-3:code:1")
    assert backend.project(PROJECT).files["ai/T-3"]["logo.png"] == bytes([0x89, 0x50, 0xFF])
    for bad in ({"encoding": "hex"}, {"content": "không phải base64!"}):
        with pytest.raises(ValueError):
            await tools.push(PROJECT, "ai/T-3", "x", [{**files[0], **bad}], "T-3:code:2")


async def test_merge_requires_server_side_approval_check() -> None:
    tools, _ = _tools(verify_ok=None)
    with pytest.raises(ConstraintError, match="chưa cấu hình"):
        await tools.merge_mr(PROJECT, 1, "A-1")
    tools, _ = _tools(verify_ok=False)
    with pytest.raises(ConstraintError, match="không hợp lệ"):
        await tools.merge_mr(PROJECT, 1, "A-1")


async def test_merge_is_idempotent() -> None:
    tools, backend = _tools()
    await tools.create_branch(PROJECT, "ai/T-2")
    await tools.push(PROJECT, "ai/T-2", "x", [{"path": "a.py", "content": "1"}], "k")
    mr = await tools.open_merge_request(PROJECT, "ai/T-2", "x")
    first = await tools.merge_mr(PROJECT, mr["iid"], "A-1")
    second = await tools.merge_mr(PROJECT, mr["iid"], "A-1")
    assert first["merged"] and first["merged_now"]
    assert second["merged"] and not second["merged_now"]
    assert backend.calls.count("merge_mr") == 1


async def test_mcp_server_exposes_tools() -> None:
    tools, _ = _tools()
    server = build_server(tools)
    async with Client(server) as client:
        listed = await client.list_tools()
        names = {t.name for t in listed.tools}
        assert {"read_file", "branch_head", "push", "open_merge_request", "merge_mr"} <= names
        result = await client.call_tool("read_file", {"project": PROJECT, "path": "app.py"})
        assert not result.is_error
        assert result.structured_content is not None
        assert result.structured_content["content"] == "print('hi')\n"
        paged = await client.call_tool(
            "read_file", {"project": PROJECT, "path": "app.py", "offset": 2, "limit": 5}
        )
        page = paged.structured_content
        assert page is not None and page["content"] == "" and page["total_lines"] == 1
        bad = await client.call_tool(
            "read_file", {"project": PROJECT, "path": "app.py", "offset": 0}
        )
        assert bad.is_error
        denied = await client.call_tool("read_file", {"project": "evil/repo", "path": "x"})
        assert denied.is_error


async def test_branch_head_returns_latest_commit() -> None:
    tools, backend = _tools()
    with pytest.raises(GitLabError):
        await tools.branch_head(PROJECT)
    backend.project(PROJECT).commits["main"] += [{"id": "a1"}, {"id": "b2"}]
    assert await tools.branch_head(PROJECT) == {"project": PROJECT, "ref": "main", "sha": "b2"}
    with pytest.raises(ConstraintError):
        await tools.branch_head("other/repo")


async def test_read_file_pages_by_line() -> None:
    tools, backend = _tools()
    backend.project(PROJECT).files["main"]["doc.md"] = "".join(f"dòng {i}\n" for i in range(1, 11))
    whole = await tools.read_file(PROJECT, "doc.md")
    assert whole["total_lines"] == 10 and "truncated" not in whole and "lines" not in whole
    part = await tools.read_file(PROJECT, "doc.md", offset=3, limit=2)
    assert part["content"] == "dòng 3\ndòng 4\n"
    assert part["lines"] == [3, 4] and part["next_offset"] == 5 and "truncated" not in part
    tail = await tools.read_file(PROJECT, "doc.md", offset=9, limit=50)
    assert tail["content"] == "dòng 9\ndòng 10\n" and "truncated" not in tail
    past = await tools.read_file(PROJECT, "doc.md", offset=99)
    assert past["content"] == "" and past["total_lines"] == 10
    with pytest.raises(ValueError):
        await tools.read_file(PROJECT, "doc.md", offset=0)
    with pytest.raises(ValueError):
        await tools.read_file(PROJECT, "doc.md", limit=0)


async def test_read_file_caps_large_file_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    """File lớn hơn giới hạn: trả từng đoạn trọn dòng, đọc hết bằng next_offset."""
    monkeypatch.setattr("mcp_gitlab.tools.MAX_READ_CHARS", 50)
    tools, backend = _tools()
    text = "".join(f"Quy tắc số {i:02d} của dự án\n" for i in range(1, 21))
    backend.project(PROJECT).files["main"]["AGENTS.md"] = text
    pieces, offset = [], 1
    while True:
        r = await tools.read_file(PROJECT, "AGENTS.md", offset=offset)
        assert len(r["content"]) <= 50 and r["content"].endswith("\n")
        pieces.append(r["content"])
        if not r.get("truncated"):
            break
        offset = r["next_offset"]
    assert "".join(pieces) == text and len(pieces) > 1


async def test_read_file_cuts_single_overlong_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mcp_gitlab.tools.MAX_READ_CHARS", 10)
    tools, backend = _tools()
    backend.project(PROJECT).files["main"]["min.js"] = "x" * 100 + "\nsau\n"
    r = await tools.read_file(PROJECT, "min.js")
    assert r["content"] == "x" * 10 and r["next_offset"] == 2
