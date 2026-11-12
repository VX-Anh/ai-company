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


def test_replay_base_branch_constraints() -> None:
    c = Constraints(
        allowed_projects=(PROJECT,),
        branch_pattern="ai/*,base/*",
        allowed_targets=("main", "base/*"),
    )
    c.check_branch("base/T-1")
    c.check_pair("ai/T-1", "base/T-1")
    c.check_pair("ai/T-1", "main")  # pure check; vào main bị chặn khi base/T-1 tồn tại (async)
    for source, target in (
        ("ai/T-2", "base/T-1"),
        ("base/T-2", "base/T-1"),
        ("base/T-1", "main"),
    ):
        with pytest.raises(ConstraintError):
            c.check_pair(source, target)
    with pytest.raises(ConstraintError):
        c.check_push("base/T-1")
    c.check_merge_target("main")
    with pytest.raises(ConstraintError):
        c.check_merge_target("base/T-1")
    with pytest.raises(ConstraintError):
        c.check_branch("feature/x")


async def test_replay_branch_cannot_open_or_merge_into_main() -> None:
    backend = FakeGitLabBackend()
    backend.project(PROJECT).files["main"]["a.py"] = "x"

    async def verify(approval_id: str, project: str, iid: int) -> tuple[bool, str]:
        return True, ""

    tools = GitLabTools(
        backend,
        Constraints(
            allowed_projects=(PROJECT,),
            branch_pattern="ai/*,base/*",
            allowed_targets=("main", "base/*"),
        ),
        verify,
    )
    await tools.create_branch(PROJECT, "ai/T-1")
    # Ticket thường (không có base/T-1): vào main được.
    mr = await tools.open_merge_request(PROJECT, "ai/T-1", "x")
    assert mr["target_branch"] == "main"
    # Ticket chạy lại: có base/T-2 thì ai/T-2 không mở MR vào main, cũng không merge được.
    await tools.create_branch(PROJECT, "base/T-2")
    await tools.create_branch(PROJECT, "ai/T-2")
    with pytest.raises(ConstraintError, match="base/T-2"):
        await tools.open_merge_request(PROJECT, "ai/T-2", "x")
    ok = await tools.open_merge_request(PROJECT, "ai/T-2", "x", target_branch="base/T-2")
    assert ok["target_branch"] == "base/T-2"
    sneaky = await backend.create_mr(PROJECT, "ai/T-2", "main", "x", "")
    with pytest.raises(ConstraintError, match="base/T-2"):
        await tools.merge_mr(PROJECT, sneaky["iid"], "A-1")


async def test_merge_base_on_fake_backend() -> None:
    tools, backend = _tools()
    await backend.commit(
        PROJECT, "main", "gốc", [{"action": "create", "file_path": "a", "content": "1"}]
    )
    fork = (await tools.branch_head(PROJECT))["sha"]
    await tools.create_branch(PROJECT, "ai/T-1")
    await tools.push(PROJECT, "ai/T-1", "sửa", [{"path": "a", "content": "2"}], "k1")
    await backend.commit(
        PROJECT, "main", "khác", [{"action": "create", "file_path": "b", "content": "1"}]
    )
    main_now = (await tools.branch_head(PROJECT))["sha"]
    assert main_now != fork
    assert (await tools.merge_base(PROJECT, "main", "ai/T-1"))["sha"] == fork
    assert (await tools.merge_base(PROJECT, main_now, "ai/T-1"))["sha"] == fork
    with pytest.raises(ConstraintError):
        await tools.merge_base("other/repo", "main", "ai/T-1")
    with pytest.raises(GitLabError):
        await tools.merge_base(PROJECT, "main", "khong-co")
    async with Client(build_server(tools)) as client:
        assert "merge_base" in {t.name for t in (await client.list_tools()).tools}


def _replay_tools(**kw: object) -> tuple[GitLabTools, FakeGitLabBackend]:
    backend = FakeGitLabBackend()
    backend.project(PROJECT).files["main"]["a.py"] = "x"
    constraints = Constraints(
        allowed_projects=(PROJECT,),
        branch_pattern="ai/*,base/*",
        allowed_targets=("main", "base/*"),
        **kw,  # type: ignore[arg-type]
    )
    return GitLabTools(backend, constraints), backend


async def test_delete_branch_pair_removes_only_replay_pair() -> None:
    tools, backend = _replay_tools()
    await tools.create_branch(PROJECT, "ai/T-1")  # ticket thường
    await tools.create_branch(PROJECT, "base/T-2")
    await tools.create_branch(PROJECT, "ai/T-2")
    await tools.push(PROJECT, "ai/T-2", "sửa", [{"path": "a.py", "content": "y"}], "k")
    head = (await tools.branch_head(PROJECT, "ai/T-2"))["sha"]
    mr = await tools.open_merge_request(PROJECT, "ai/T-2", "x", target_branch="base/T-2")

    got = await tools.delete_branch_pair(PROJECT, "ai/T-2")
    assert got["deleted"] == ["ai/T-2", "base/T-2"] and got["heads"]["ai/T-2"] == head
    # Nhánh làm việc xoá trước nhánh gốc: không có lúc nào ai/T-2 tồn tại mà thiếu base/T-2.
    deletes = [c for c in backend.calls if c.startswith("delete_branch")]
    assert deletes == ["delete_branch:ai/T-2", "delete_branch:base/T-2"]
    assert set(backend.project(PROJECT).files) == {"main", "ai/T-1"}
    assert (await tools.get_merge_request(PROJECT, mr["iid"]))["state"] == "closed"
    # Gọi lại: không còn gì để xoá, không lỗi.
    assert (await tools.delete_branch_pair(PROJECT, "ai/T-2"))["deleted"] == []


@pytest.mark.parametrize(
    "branch",
    ["main", "base/T-2", "ai/", "ai", "ai/T-2/x", "ai/../main", "feature/x", "", "refs/heads/main"],
)
async def test_delete_branch_pair_refuses_anything_but_ai_id(branch: str) -> None:
    tools, backend = _replay_tools()
    await tools.create_branch(PROJECT, "base/T-2")
    await tools.create_branch(PROJECT, "ai/T-2")
    with pytest.raises(ConstraintError):
        await tools.delete_branch_pair(PROJECT, branch)
    assert set(backend.project(PROJECT).files) == {"main", "ai/T-2", "base/T-2"}
    assert not any(c.startswith("delete_branch") for c in backend.calls)


async def test_delete_branch_pair_refuses_normal_ticket_branch_and_other_repo() -> None:
    tools, backend = _replay_tools()
    await tools.create_branch(PROJECT, "ai/T-1")
    with pytest.raises(ConstraintError, match="ticket thường"):
        await tools.delete_branch_pair(PROJECT, "ai/T-1")
    with pytest.raises(ConstraintError):
        await tools.delete_branch_pair("other/repo", "ai/T-1")
    # base/T-10 không phải nhánh gốc của ai/T-1 (so khớp cả tên, không theo tiền tố).
    await tools.create_branch(PROJECT, "base/T-10")
    with pytest.raises(ConstraintError, match="ticket thường"):
        await tools.delete_branch_pair(PROJECT, "ai/T-1")
    assert "ai/T-1" in backend.project(PROJECT).files
    assert not any(c.startswith("delete_branch") for c in backend.calls)


async def test_delete_branch_pair_needs_base_pattern_and_never_merge_target() -> None:
    # Server chỉ cho ai/* (mặc định, chưa bật chạy lại): không có cặp nào để xoá.
    tools, backend = _tools()
    await backend.create_branch(PROJECT, "base/T-2", "main")
    await backend.create_branch(PROJECT, "ai/T-2", "main")
    with pytest.raises(ConstraintError):
        await tools.delete_branch_pair(PROJECT, "ai/T-2")
    # Cấu hình sai (base/* là đích merge): vẫn không xoá nhánh là đích merge.
    odd, odd_backend = _replay_tools(merge_targets=("main", "base/*"))
    await odd.create_branch(PROJECT, "base/T-2")
    await odd.create_branch(PROJECT, "ai/T-2")
    with pytest.raises(ConstraintError, match="không bao giờ xoá"):
        await odd.delete_branch_pair(PROJECT, "ai/T-2")
    assert "base/T-2" in odd_backend.project(PROJECT).files


async def test_delete_branch_pair_refuses_open_mr_to_other_target() -> None:
    tools, backend = _replay_tools()
    await tools.create_branch(PROJECT, "base/T-2")
    await tools.create_branch(PROJECT, "ai/T-2")
    await backend.create_mr(PROJECT, "ai/T-2", "main", "x", "")  # MR lọt vào main từ ngoài aico
    with pytest.raises(ConstraintError, match="MR mở"):
        await tools.delete_branch_pair(PROJECT, "ai/T-2")
    assert {"ai/T-2", "base/T-2"} <= set(backend.project(PROJECT).files)


async def test_delete_branch_pair_resumes_after_partial_failure() -> None:
    tools, backend = _replay_tools()
    await tools.create_branch(PROJECT, "base/T-2")
    await tools.create_branch(PROJECT, "ai/T-2")
    # Lỗi ở nhánh làm việc: nhánh gốc còn nguyên (ai/T-2 vẫn bị chặn vào main).
    backend.delete_fails = {"ai/T-2"}
    with pytest.raises(GitLabError):
        await tools.delete_branch_pair(PROJECT, "ai/T-2")
    assert {"ai/T-2", "base/T-2"} <= set(backend.project(PROJECT).files)
    # Lỗi ở nhánh gốc: gọi lại xoá nốt base/T-2 mồ côi.
    backend.delete_fails = {"base/T-2"}
    with pytest.raises(GitLabError):
        await tools.delete_branch_pair(PROJECT, "ai/T-2")
    assert "ai/T-2" not in backend.project(PROJECT).files
    backend.delete_fails = set()
    assert (await tools.delete_branch_pair(PROJECT, "ai/T-2"))["deleted"] == ["base/T-2"]
    assert set(backend.project(PROJECT).files) == {"main"}
