"""Truy cập GitLab REST API. Bản Fake dùng cho test và chạy thử không cần GitLab."""

from __future__ import annotations

import base64
import io
import tarfile
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote

import httpx


class GitLabError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitLabBackend(Protocol):
    async def get_file(self, project: str, path: str, ref: str) -> str: ...

    async def archive(self, project: str, ref: str) -> bytes:
        """Mã nguồn tại ref dạng tar.gz (một thư mục gốc)."""
        ...

    async def branch_exists(self, project: str, branch: str) -> bool: ...

    async def create_branch(self, project: str, branch: str, ref: str) -> dict[str, Any]: ...

    async def recent_commits(self, project: str, branch: str) -> list[dict[str, Any]]: ...

    async def commit(
        self, project: str, branch: str, message: str, actions: list[dict[str, Any]]
    ) -> dict[str, Any]: ...

    async def list_issues(self, project: str, labels: list[str]) -> list[dict[str, Any]]: ...

    async def find_open_mr(self, project: str, source_branch: str) -> dict[str, Any] | None: ...

    async def create_mr(
        self, project: str, source_branch: str, target_branch: str, title: str, description: str
    ) -> dict[str, Any]: ...

    async def get_mr(self, project: str, iid: int) -> dict[str, Any]: ...

    async def merge_mr(self, project: str, iid: int) -> dict[str, Any]: ...


def _pid(project: str) -> str:
    return quote(project, safe="")


class HttpGitLabBackend:
    def __init__(
        self, base_url: str, token: str, *, merge_token: str | None = None, timeout: float = 30
    ) -> None:
        self._api = base_url.rstrip("/") + "/api/v4"
        self._client = httpx.AsyncClient(timeout=timeout, headers={"PRIVATE-TOKEN": token})
        self._merge_headers = {"PRIVATE-TOKEN": merge_token} if merge_token else None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self, method: str, path: str, *, headers: dict[str, str] | None = None, **kw: Any
    ) -> Any:
        resp = await self._client.request(method, self._api + path, headers=headers, **kw)
        if resp.status_code >= 400:
            raise GitLabError(
                f"GitLab {method} {path}: {resp.status_code} {resp.text[:200]}", resp.status_code
            )
        return resp.json() if resp.content else {}

    async def get_file(self, project: str, path: str, ref: str) -> str:
        data = await self._request(
            "GET",
            f"/projects/{_pid(project)}/repository/files/{quote(path, safe='')}",
            params={"ref": ref},
        )
        return base64.b64decode(data["content"]).decode("utf-8")

    async def archive(self, project: str, ref: str) -> bytes:
        path = f"/projects/{_pid(project)}/repository/archive.tar.gz"
        resp = await self._client.get(self._api + path, params={"sha": ref})
        if resp.status_code >= 400:
            raise GitLabError(f"GitLab GET {path}: {resp.status_code}", resp.status_code)
        return resp.content

    async def branch_exists(self, project: str, branch: str) -> bool:
        try:
            await self._request(
                "GET", f"/projects/{_pid(project)}/repository/branches/{quote(branch, safe='')}"
            )
        except GitLabError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    async def create_branch(self, project: str, branch: str, ref: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/projects/{_pid(project)}/repository/branches",
            params={"branch": branch, "ref": ref},
        )

    async def recent_commits(self, project: str, branch: str) -> list[dict[str, Any]]:
        return await self._request(
            "GET",
            f"/projects/{_pid(project)}/repository/commits",
            params={"ref_name": branch, "per_page": 20},
        )

    async def commit(
        self, project: str, branch: str, message: str, actions: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/projects/{_pid(project)}/repository/commits",
            json={"branch": branch, "commit_message": message, "actions": actions},
        )

    async def list_issues(self, project: str, labels: list[str]) -> list[dict[str, Any]]:
        return await self._request(
            "GET",
            f"/projects/{_pid(project)}/issues",
            params={"state": "opened", "labels": ",".join(labels), "per_page": 50},
        )

    async def find_open_mr(self, project: str, source_branch: str) -> dict[str, Any] | None:
        items = await self._request(
            "GET",
            f"/projects/{_pid(project)}/merge_requests",
            params={"state": "opened", "source_branch": source_branch},
        )
        return items[0] if items else None

    async def create_mr(
        self, project: str, source_branch: str, target_branch: str, title: str, description: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/projects/{_pid(project)}/merge_requests",
            json={
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": title,
                "description": description,
                "remove_source_branch": True,
            },
        )

    async def get_mr(self, project: str, iid: int) -> dict[str, Any]:
        return await self._request("GET", f"/projects/{_pid(project)}/merge_requests/{iid}")

    async def merge_mr(self, project: str, iid: int) -> dict[str, Any]:
        if self._merge_headers is None:
            raise GitLabError("chưa cấu hình token của merge bot")
        return await self._request(
            "PUT",
            f"/projects/{_pid(project)}/merge_requests/{iid}/merge",
            headers=self._merge_headers,
        )


@dataclass
class _FakeProject:
    files: dict[str, dict[str, str | bytes]] = field(default_factory=lambda: {"main": {}})
    commits: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: {"main": []})
    issues: list[dict[str, Any]] = field(default_factory=list)
    mrs: list[dict[str, Any]] = field(default_factory=list)


class FakeGitLabBackend:
    """GitLab giả trong bộ nhớ. `merge_fails` mô phỏng conflict hoặc pipeline lỗi."""

    def __init__(self) -> None:
        self.projects: dict[str, _FakeProject] = {}
        self.merge_fails = 0
        self.calls: list[str] = []

    def project(self, name: str) -> _FakeProject:
        return self.projects.setdefault(name, _FakeProject())

    async def get_file(self, project: str, path: str, ref: str) -> str:
        self.calls.append("get_file")
        try:
            content = self.project(project).files[ref][path]
        except KeyError:
            raise GitLabError(f"không có file {path}@{ref}", 404) from None
        return content.decode("utf-8") if isinstance(content, bytes) else content

    async def archive(self, project: str, ref: str) -> bytes:
        self.calls.append("archive")
        files = self.project(project).files.get(ref)
        if files is None:
            raise GitLabError(f"không có ref {ref}", 404)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for path, content in files.items():
                data = content if isinstance(content, bytes) else content.encode("utf-8")
                info = tarfile.TarInfo(f"{project.replace('/', '-')}-{ref}/{path}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    async def branch_exists(self, project: str, branch: str) -> bool:
        return branch in self.project(project).files

    async def create_branch(self, project: str, branch: str, ref: str) -> dict[str, Any]:
        self.calls.append("create_branch")
        p = self.project(project)
        if branch in p.files:
            raise GitLabError("branch đã tồn tại", 400)
        p.files[branch] = dict(p.files[ref])
        p.commits[branch] = list(p.commits[ref])
        return {"name": branch}

    async def recent_commits(self, project: str, branch: str) -> list[dict[str, Any]]:
        return list(reversed(self.project(project).commits.get(branch, [])))[:20]

    async def commit(
        self, project: str, branch: str, message: str, actions: list[dict[str, Any]]
    ) -> dict[str, Any]:
        self.calls.append("commit")
        p = self.project(project)
        files = p.files[branch]
        for action in actions:
            if action["action"] == "delete":
                files.pop(action["file_path"], None)
            elif action.get("encoding") == "base64":
                files[action["file_path"]] = base64.b64decode(action["content"])
            else:
                files[action["file_path"]] = action["content"]
        sha = f"{len(self.calls):040x}"
        p.commits[branch].append({"id": sha, "message": message})
        return {"id": sha}

    async def list_issues(self, project: str, labels: list[str]) -> list[dict[str, Any]]:
        return [i for i in self.project(project).issues if set(labels) <= set(i["labels"])]

    async def find_open_mr(self, project: str, source_branch: str) -> dict[str, Any] | None:
        return next(
            (
                m
                for m in self.project(project).mrs
                if m["source_branch"] == source_branch and m["state"] == "opened"
            ),
            None,
        )

    async def create_mr(
        self, project: str, source_branch: str, target_branch: str, title: str, description: str
    ) -> dict[str, Any]:
        self.calls.append("create_mr")
        p = self.project(project)
        mr = {
            "iid": len(p.mrs) + 1,
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
            "state": "opened",
            "web_url": f"https://gitlab.example/{project}/-/merge_requests/{len(p.mrs) + 1}",
        }
        p.mrs.append(mr)
        return dict(mr)

    async def get_mr(self, project: str, iid: int) -> dict[str, Any]:
        for mr in self.project(project).mrs:
            if mr["iid"] == iid:
                return dict(mr)
        raise GitLabError(f"không có MR !{iid}", 404)

    async def merge_mr(self, project: str, iid: int) -> dict[str, Any]:
        self.calls.append("merge_mr")
        if self.merge_fails > 0:
            self.merge_fails -= 1
            raise GitLabError("merge thất bại: conflict", 405)
        for mr in self.project(project).mrs:
            if mr["iid"] == iid:
                mr["state"] = "merged"
                return dict(mr)
        raise GitLabError(f"không có MR !{iid}", 404)
