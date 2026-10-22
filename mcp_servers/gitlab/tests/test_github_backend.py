"""GitLabTools chạy trên backend GitHub (API giả bằng MockTransport)."""

from __future__ import annotations

import base64
import io
import itertools
import json
import re
import tarfile
from typing import Any

import httpx
import pytest
from mcp.client.client import Client

from mcp_gitlab.backend import FakeGitLabBackend
from mcp_gitlab.github import HttpGitHubBackend
from mcp_gitlab.server import build_server
from mcp_gitlab.tools import Constraints, GitLabTools

REPO = "acme/pilot"


class FakeGitHub:
    """Đủ API cho luồng: đọc file, branch, commit qua Git Data API, PR, issue, merge."""

    def __init__(self) -> None:
        ids = itertools.count(1)
        self.next_sha = lambda: f"{next(ids):040x}"
        root = self.next_sha()
        self.commits: dict[str, dict[str, Any]] = {
            root: {"message": "init", "files": {"README.md": "# pilot\n"}, "parents": []}
        }
        self.trees: dict[str, dict[str, Any]] = {}
        self.blobs: dict[str, bytes] = {}
        self.tarball_cache: dict[str, str] = {}
        self.refs = {"main": root}
        self.pulls: list[dict[str, Any]] = []
        self.issues = [
            {
                "number": 1,
                "title": "Lỗi",
                "body": "chi tiết",
                "labels": [{"name": "ai-company"}],
                "html_url": "u1",
            },
            {"number": 2, "title": "PR", "labels": [{"name": "ai-company"}], "pull_request": {}},
        ]
        self.auth: list[str] = []
        self.ref_lag = 0  # số lần đọc ref mới tạo còn 404 (bản sao GitHub chưa đồng bộ)
        self._lagging: dict[str, int] = {}

    def _files(self, branch: str) -> dict[str, str]:
        return self.commits[self.refs[branch]]["files"]

    def handler(self, request: httpx.Request) -> httpx.Response:  # noqa: C901
        self.auth.append(request.headers["authorization"])
        path = request.url.path.removeprefix(f"/repos/{REPO}")
        body = json.loads(request.content) if request.content else {}
        m, p = request.method, path
        if m == "GET" and (g := re.fullmatch(r"/contents/(.+)", p)):
            files = self._files(request.url.params["ref"])
            if g[1] not in files:
                return httpx.Response(404, json={"message": "Not Found"})
            content = base64.b64encode(files[g[1]].encode()).decode()
            return httpx.Response(200, json={"type": "file", "content": content})
        if m == "GET" and (g := re.fullmatch(r"/git/ref/heads/(.+)", p)):
            if g[1] not in self.refs or self._lagging.get(g[1], 0) > 0:
                if g[1] in self._lagging:
                    self._lagging[g[1]] -= 1
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"object": {"sha": self.refs[g[1]]}})
        if m == "POST" and p == "/git/refs":
            name = body["ref"].removeprefix("refs/heads/")
            self.refs[name] = body["sha"]
            self._lagging[name] = self.ref_lag
            return httpx.Response(201, json={})
        if m == "GET" and (g := re.fullmatch(r"/tarball/(.+)", p)):
            # codeload cache: theo tên branch trả commit lúc tải lần đầu, theo SHA thì luôn đúng.
            sha = (
                g[1]
                if g[1] in self.commits
                else self.tarball_cache.setdefault(g[1], self.refs[g[1]])
            )
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                for name, content in self.commits[sha]["files"].items():
                    data = content if isinstance(content, bytes) else content.encode()
                    info = tarfile.TarInfo(f"repo-{sha[:7]}/{name}")
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            return httpx.Response(200, content=buf.getvalue())
        if m == "GET" and p == "/commits":
            sha, out = self.refs[request.url.params["sha"]], []
            while sha:
                c = self.commits[sha]
                out.append({"sha": sha, "commit": {"message": c["message"]}})
                sha = c["parents"][0] if c["parents"] else None
            return httpx.Response(200, json=out)
        if m == "GET" and (g := re.fullmatch(r"/git/commits/(\w+)", p)):
            tree = f"tree-{g[1]}"
            self.trees[tree] = dict(self.commits[g[1]]["files"])
            return httpx.Response(200, json={"tree": {"sha": tree}})
        if m == "POST" and p == "/git/trees":
            files = dict(self.trees[body["base_tree"]])
            for e in body["tree"]:
                if e.get("sha", "") is None:
                    files.pop(e["path"], None)
                elif "sha" in e:
                    files[e["path"]] = self.blobs[e["sha"]]
                else:
                    files[e["path"]] = e["content"]
            sha = f"tree-new-{len(self.trees)}"
            self.trees[sha] = files
            return httpx.Response(201, json={"sha": sha})
        if m == "POST" and p == "/git/blobs":
            assert body["encoding"] == "base64"
            sha = f"blob-{len(self.blobs)}"
            self.blobs[sha] = base64.b64decode(body["content"])
            return httpx.Response(201, json={"sha": sha})
        if m == "POST" and p == "/git/commits":
            sha = self.next_sha()
            self.commits[sha] = {
                "message": body["message"],
                "files": self.trees[body["tree"]],
                "parents": body["parents"],
            }
            return httpx.Response(201, json={"sha": sha})
        if m == "PATCH" and (g := re.fullmatch(r"/git/refs/heads/(.+)", p)):
            self.refs[g[1]] = body["sha"]
            return httpx.Response(200, json={})
        if m == "DELETE" and (g := re.fullmatch(r"/git/refs/heads/(.+)", p)):
            self.refs.pop(g[1], None)
            return httpx.Response(204)
        if m == "GET" and p == "/issues":
            return httpx.Response(200, json=self.issues)
        if m == "GET" and p == "/pulls":
            head = request.url.params["head"].split(":", 1)[1]
            return httpx.Response(
                200,
                json=[
                    pr for pr in self.pulls if pr["head"]["ref"] == head and pr["state"] == "open"
                ],
            )
        if m == "POST" and p == "/pulls":
            pr = {
                "number": len(self.pulls) + 10,
                "state": "open",
                "merged": False,
                "title": body["title"],
                "body": body["body"],
                "head": {"ref": body["head"]},
                "base": {"ref": body["base"]},
                "html_url": f"https://github.com/{REPO}/pull/{len(self.pulls) + 10}",
            }
            self.pulls.append(pr)
            return httpx.Response(201, json=pr)
        if (g := re.fullmatch(r"/pulls/(\d+)(/merge)?", p)) and m in ("GET", "PUT"):
            pr = next(pr for pr in self.pulls if pr["number"] == int(g[1]))
            if m == "PUT":
                pr.update(state="closed", merged=True)
                self.refs[pr["base"]["ref"]] = self.refs[pr["head"]["ref"]]
                return httpx.Response(200, json={"merged": True})
            return httpx.Response(200, json=pr)
        return httpx.Response(500, json={"message": f"fake chưa hỗ trợ {m} {p}"})


def _tools(gh: FakeGitHub) -> GitLabTools:
    backend = HttpGitHubBackend(
        "dev-token", merge_token="merge-token", transport=httpx.MockTransport(gh.handler)
    )

    async def verify(approval_id: str, project: str, mr_iid: int) -> tuple[bool, str]:
        return True, "ok"

    return GitLabTools(backend, Constraints(allowed_projects=(REPO,)), verify)


async def test_full_flow_on_github() -> None:
    gh = FakeGitHub()
    tools = _tools(gh)
    assert (await tools.read_file(REPO, "README.md"))["content"] == "# pilot\n"
    issues = (await tools.list_issues(REPO, ["ai-company"]))["issues"]
    assert [(i["iid"], i["title"]) for i in issues] == [(1, "Lỗi")]  # PR bị lọc ra

    assert (await tools.create_branch(REPO, "ai/T-1"))["created"]
    assert not (await tools.create_branch(REPO, "ai/T-1"))["created"]
    files = [
        {"path": "README.md", "content": "# pilot\n\n## Cài đặt\n"},
        {"path": "NOTES.md", "content": "x"},
    ]
    first = await tools.push(REPO, "ai/T-1", "Thêm mục cài đặt", files, "T-1:code:1")
    again = await tools.push(REPO, "ai/T-1", "Thêm mục cài đặt", files, "T-1:code:1")
    assert first["created"] and not again["created"] and first["commit"] == again["commit"]
    assert gh._files("ai/T-1")["README.md"].endswith("## Cài đặt\n")  # noqa: SLF001
    assert "README.md" in gh._files("main")  # noqa: SLF001

    mr = await tools.open_merge_request(REPO, "ai/T-1", "T-1: cài đặt")
    same = await tools.open_merge_request(REPO, "ai/T-1", "T-1: cài đặt")
    assert mr["created"] and not same["created"] and mr["iid"] == same["iid"]
    assert mr["state"] == "opened" and not mr["merged"]

    merged = await tools.merge_mr(REPO, mr["iid"], "A-1")
    assert merged["merged"] and merged["merged_now"]
    assert gh._files("main")["NOTES.md"] == "x"  # noqa: SLF001
    assert "ai/T-1" not in gh.refs  # branch nguồn đã xóa sau merge
    assert "Bearer merge-token" in gh.auth and gh.auth[0] == "Bearer dev-token"


async def test_push_right_after_create_branch_survives_github_lag() -> None:
    gh = FakeGitHub()
    gh.ref_lag = 3  # ref mới tạo: 3 lần đọc đầu vẫn 404
    tools = _tools(gh)
    assert (await tools.create_branch(REPO, "ai/T-2"))["created"]
    files = [{"path": "NOTES.md", "content": "x"}]
    assert (await tools.push(REPO, "ai/T-2", "Ghi chú", files, "T-2:code:1"))["created"]


async def test_push_binary_file_creates_blob() -> None:
    gh = FakeGitHub()
    tools = _tools(gh)
    await tools.create_branch(REPO, "ai/T-3")
    png = base64.b64encode(bytes([0x89, 0x50, 0xFF])).decode()
    files = [
        {"path": "logo.png", "action": "create", "content": png, "encoding": "base64"},
        {"path": "NOTES.md", "content": "x"},
    ]
    assert (await tools.push(REPO, "ai/T-3", "Thêm logo", files, "T-3:code:1"))["created"]
    assert gh._files("ai/T-3")["logo.png"] == bytes([0x89, 0x50, 0xFF])  # noqa: SLF001
    assert gh._files("ai/T-3")["NOTES.md"] == "x"  # noqa: SLF001


async def test_archive_after_push_is_fresh_despite_codeload_cache() -> None:
    gh = FakeGitHub()
    tools = _tools(gh)
    await tools.create_branch(REPO, "ai/T-4")

    async def readme() -> str:
        archive = await tools.download_archive(REPO, "ai/T-4")
        raw = base64.b64decode(archive["data"])
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            member = next(m for m in tar.getmembers() if m.name.endswith("/README.md"))
            return tar.extractfile(member).read().decode()  # type: ignore[union-attr]

    assert await readme() == "# pilot\n"
    files = [{"path": "README.md", "content": "# v2\n"}]
    await tools.push(REPO, "ai/T-4", "Sửa", files, "T-4:code:1")
    assert await readme() == "# v2\n"  # tải theo tên branch sẽ trả bản cũ trong cache
    assert gh.tarball_cache == {}


async def test_missing_file_message_reaches_agent() -> None:
    """Lỗi đã lường trước phải tới được agent, không bị che thành 'Error executing tool'."""
    gh_tools = _tools(FakeGitHub())
    gl = FakeGitLabBackend()
    gl_tools = GitLabTools(gl, Constraints(allowed_projects=("g/p",)))
    for tools, project in ((gh_tools, REPO), (gl_tools, "g/p")):
        async with Client(build_server(tools)) as client:
            result = await client.call_tool("read_file", {"project": project, "path": "nope.py"})
            text = result.content[0].text  # type: ignore[union-attr]
            assert result.is_error and ("404" in text or "không có file" in text), text
            denied = await client.call_tool("read_file", {"project": "evil/x", "path": "a"})
            assert "không nằm trong danh sách" in denied.content[0].text  # type: ignore[union-attr]


def test_project_must_be_owner_repo() -> None:
    from mcp_gitlab.backend import GitLabError
    from mcp_gitlab.github import _repo

    with pytest.raises(GitLabError):
        _repo("group/sub/repo")
