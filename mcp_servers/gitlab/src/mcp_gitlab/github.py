"""Backend GitHub REST API, cùng giao diện GitLabBackend để dùng chung GitLabTools.

Quy đổi sang khái niệm GitLab mà phần còn lại của hệ thống dùng:
- project = "owner/repo"; merge request = pull request; iid = số PR/issue
- state PR: open -> "opened", đã merge -> "merged", đóng không merge -> "closed"
- commit nhiều file một lần qua Git Data API (tree + commit + cập nhật ref)
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from typing import Any
from urllib.parse import quote

import httpx

from mcp_gitlab.backend import GitLabError


def _repo(project: str) -> str:
    owner, _, name = project.partition("/")
    if not owner or not name or "/" in name:
        raise GitLabError(f"repo GitHub phải có dạng owner/repo: '{project}'", 400)
    return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"


def _pr_view(pr: dict[str, Any]) -> dict[str, Any]:
    if pr.get("merged") or pr.get("merged_at"):
        state = "merged"
    elif pr.get("state") == "open":
        state = "opened"
    else:
        state = "closed"
    return {
        "iid": pr["number"],
        "state": state,
        "source_branch": pr["head"]["ref"],
        "target_branch": pr["base"]["ref"],
        "title": pr.get("title"),
        "description": pr.get("body") or "",
        "web_url": pr.get("html_url"),
    }


class HttpGitHubBackend:
    def __init__(
        self,
        token: str,
        *,
        merge_token: str | None = None,
        api_url: str = "https://api.github.com",
        timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,  # test: GitHub giả
        ref_visible_seconds: float = 15,
    ) -> None:
        self._api = api_url.rstrip("/")
        self._ref_visible_seconds = ref_visible_seconds
        base_headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={**base_headers, "Authorization": f"Bearer {token}"},
            transport=transport,
        )
        self._merge_headers = {"Authorization": f"Bearer {merge_token}"} if merge_token else None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self, method: str, path: str, *, headers: dict[str, str] | None = None, **kw: Any
    ) -> Any:
        resp = await self._client.request(method, self._api + path, headers=headers, **kw)
        if resp.status_code >= 400:
            raise GitLabError(
                f"GitHub {method} {path}: {resp.status_code} {resp.text[:200]}", resp.status_code
            )
        return resp.json() if resp.content else {}

    async def _head_sha(self, project: str, branch: str) -> str:
        ref = await self._request(
            "GET", f"{_repo(project)}/git/ref/heads/{quote(branch, safe='/')}"
        )
        return ref["object"]["sha"]

    async def get_file(self, project: str, path: str, ref: str) -> str:
        data = await self._request(
            "GET", f"{_repo(project)}/contents/{quote(path, safe='/')}", params={"ref": ref}
        )
        if isinstance(data, list) or data.get("type") != "file":
            raise GitLabError(f"'{path}' không phải file", 400)
        return base64.b64decode(data["content"]).decode("utf-8")

    async def archive(self, project: str, ref: str) -> bytes:
        path = f"{_repo(project)}/tarball/{quote(ref, safe='/')}"
        # GitHub chuyển hướng sang codeload (URL đã kèm quyền tạm cho repo private).
        resp = await self._client.get(self._api + path, follow_redirects=True)
        if resp.status_code >= 400:
            raise GitLabError(f"GitHub GET {path}: {resp.status_code}", resp.status_code)
        return resp.content

    async def branch_exists(self, project: str, branch: str) -> bool:
        try:
            await self._head_sha(project, branch)
        except GitLabError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    async def create_branch(self, project: str, branch: str, ref: str) -> dict[str, Any]:
        sha = await self._head_sha(project, ref)
        await self._request(
            "POST", f"{_repo(project)}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha}
        )
        # GitHub trả 201 nhưng vài giây sau đọc ref vẫn có thể 404 (bản sao chưa đồng bộ):
        # chờ tới khi đọc được để bước push ngay sau không báo "branch chưa tồn tại".
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._ref_visible_seconds
        delay = 0.25
        while not await self.branch_exists(project, branch):
            if loop.time() + delay > deadline:
                raise GitLabError(f"đã tạo branch '{branch}' nhưng GitHub chưa thấy sau chờ", 503)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 2)
        return {"name": branch}

    async def recent_commits(self, project: str, branch: str) -> list[dict[str, Any]]:
        items = await self._request(
            "GET", f"{_repo(project)}/commits", params={"sha": branch, "per_page": 20}
        )
        return [{"id": c["sha"], "message": c["commit"]["message"]} for c in items]

    async def commit(
        self, project: str, branch: str, message: str, actions: list[dict[str, Any]]
    ) -> dict[str, Any]:
        repo = _repo(project)
        parent = await self._head_sha(project, branch)
        base_tree = (await self._request("GET", f"{repo}/git/commits/{parent}"))["tree"]["sha"]
        tree: list[dict[str, Any]] = []
        for a in actions:
            entry: dict[str, Any] = {"path": a["file_path"], "mode": "100644", "type": "blob"}
            if a["action"] == "delete":
                entry["sha"] = None
            elif a.get("encoding") == "base64":
                # Tree chỉ nhận content UTF-8: file nhị phân phải tạo blob trước.
                blob = await self._request(
                    "POST",
                    f"{repo}/git/blobs",
                    json={"content": a["content"], "encoding": "base64"},
                )
                entry["sha"] = blob["sha"]
            else:
                entry["content"] = a["content"]
            tree.append(entry)
        new_tree = await self._request(
            "POST", f"{repo}/git/trees", json={"base_tree": base_tree, "tree": tree}
        )
        commit = await self._request(
            "POST",
            f"{repo}/git/commits",
            json={"message": message, "tree": new_tree["sha"], "parents": [parent]},
        )
        await self._request(
            "PATCH",
            f"{repo}/git/refs/heads/{quote(branch, safe='/')}",
            json={"sha": commit["sha"], "force": False},
        )
        return {"id": commit["sha"]}

    async def list_issues(self, project: str, labels: list[str]) -> list[dict[str, Any]]:
        items = await self._request(
            "GET",
            f"{_repo(project)}/issues",
            params={"state": "open", "labels": ",".join(labels), "per_page": 50},
        )
        return [
            {
                "iid": i["number"],
                "title": i["title"],
                "description": i.get("body") or "",
                "labels": [lb["name"] for lb in i.get("labels", [])],
                "web_url": i.get("html_url"),
            }
            for i in items
            if "pull_request" not in i  # API issues của GitHub trả cả PR
        ]

    async def find_open_mr(self, project: str, source_branch: str) -> dict[str, Any] | None:
        owner = project.split("/", 1)[0]
        items = await self._request(
            "GET",
            f"{_repo(project)}/pulls",
            params={"state": "open", "head": f"{owner}:{source_branch}"},
        )
        return _pr_view(items[0]) if items else None

    async def create_mr(
        self, project: str, source_branch: str, target_branch: str, title: str, description: str
    ) -> dict[str, Any]:
        pr = await self._request(
            "POST",
            f"{_repo(project)}/pulls",
            json={
                "title": title,
                "head": source_branch,
                "base": target_branch,
                "body": description,
            },
        )
        return _pr_view(pr)

    async def get_mr(self, project: str, iid: int) -> dict[str, Any]:
        return _pr_view(await self._request("GET", f"{_repo(project)}/pulls/{iid}"))

    async def merge_mr(self, project: str, iid: int) -> dict[str, Any]:
        if self._merge_headers is None:
            raise GitLabError("chưa cấu hình token của merge bot")
        await self._request(
            "PUT", f"{_repo(project)}/pulls/{iid}/merge", headers=self._merge_headers
        )
        pr = await self.get_mr(project, iid)
        # Như remove_source_branch của GitLab; không xóa được thì bỏ qua.
        with contextlib.suppress(GitLabError):
            await self._request(
                "DELETE",
                f"{_repo(project)}/git/refs/heads/{quote(pr['source_branch'], safe='/')}",
                headers=self._merge_headers,
            )
        return pr
