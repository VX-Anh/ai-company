from __future__ import annotations

import pytest
from mcp.client.client import Client

from mcp_gitlab.backend import FakeGitLabBackend
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
        assert {"read_file", "push", "open_merge_request", "merge_mr"} <= names
        result = await client.call_tool("read_file", {"project": PROJECT, "path": "app.py"})
        assert not result.is_error
        assert result.structured_content is not None
        assert result.structured_content["content"] == "print('hi')\n"
        denied = await client.call_tool("read_file", {"project": "evil/repo", "path": "x"})
        assert denied.is_error
