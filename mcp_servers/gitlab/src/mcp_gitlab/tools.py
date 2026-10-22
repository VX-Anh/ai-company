"""Logic tool GitLab. Server tự kiểm tra repo, branch và bản ghi duyệt, không chỉ dựa vào guard."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

from mcp_gitlab.backend import GitLabBackend, GitLabError

IDEMPOTENCY_TRAILER = "Idempotency-Key:"
MAX_ARCHIVE_BYTES = 25_000_000

# (approval_id, project, mr_iid) -> (hợp lệ, lý do)
ApprovalVerifier = Callable[[str, str, int], Awaitable[tuple[bool, str]]]


class ConstraintError(PermissionError):
    pass


@dataclass(frozen=True)
class Constraints:
    allowed_projects: tuple[str, ...]
    branch_pattern: str = "ai/*"
    allowed_targets: tuple[str, ...] = ("main",)

    def check_project(self, project: str) -> None:
        if project not in self.allowed_projects:
            raise ConstraintError(f"repo '{project}' không nằm trong danh sách cho phép")

    def check_branch(self, branch: str) -> None:
        bad = not branch or ".." in branch or branch.startswith("/") or branch.endswith("/")
        if bad or not fnmatchcase(branch, self.branch_pattern):
            raise ConstraintError(f"branch '{branch}' không khớp '{self.branch_pattern}'")

    def check_target(self, branch: str) -> None:
        if branch not in self.allowed_targets:
            raise ConstraintError(f"không được mở MR vào '{branch}'")


def _mr_view(mr: dict[str, Any]) -> dict[str, Any]:
    return {
        "iid": mr["iid"],
        "state": mr["state"],
        "merged": mr["state"] == "merged",
        "source_branch": mr.get("source_branch"),
        "target_branch": mr.get("target_branch"),
        "web_url": mr.get("web_url"),
    }


class GitLabTools:
    def __init__(
        self,
        backend: GitLabBackend,
        constraints: Constraints,
        verify_approval: ApprovalVerifier | None = None,
    ) -> None:
        self._backend = backend
        self._c = constraints
        self._verify = verify_approval

    async def read_file(self, project: str, path: str, ref: str = "main") -> dict[str, Any]:
        self._c.check_project(project)
        return {
            "path": path,
            "ref": ref,
            "content": await self._backend.get_file(project, path, ref),
        }

    async def download_archive(self, project: str, ref: str = "main") -> dict[str, Any]:
        """Mã nguồn tại ref (tar.gz, base64) để worker sửa code trong thư mục tạm."""
        self._c.check_project(project)
        data = await self._backend.archive(project, ref)
        if len(data) > MAX_ARCHIVE_BYTES:
            raise ConstraintError(f"repo quá lớn ({len(data)} byte) để tải về worker")
        return {
            "project": project,
            "ref": ref,
            "format": "tar.gz",
            "size": len(data),
            "data": base64.b64encode(data).decode("ascii"),
        }

    async def list_issues(self, project: str, labels: list[str]) -> dict[str, Any]:
        self._c.check_project(project)
        issues = await self._backend.list_issues(project, labels)
        return {
            "issues": [
                {
                    "iid": i["iid"],
                    "title": i["title"],
                    "description": i.get("description") or "",
                    "labels": i.get("labels", []),
                    "web_url": i.get("web_url"),
                }
                for i in issues
            ]
        }

    async def create_branch(self, project: str, branch: str, ref: str = "main") -> dict[str, Any]:
        self._c.check_project(project)
        self._c.check_branch(branch)
        if await self._backend.branch_exists(project, branch):
            return {"branch": branch, "created": False}
        await self._backend.create_branch(project, branch, ref)
        return {"branch": branch, "created": True}

    async def push(
        self,
        project: str,
        branch: str,
        commit_message: str,
        files: list[dict[str, Any]],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Tạo một commit trên branch bằng API (không cần git hay credential trong sandbox)."""
        self._c.check_project(project)
        self._c.check_branch(branch)
        if not idempotency_key:
            raise ValueError("cần idempotency_key")
        if not files:
            raise ValueError("không có file nào để commit")
        if not await self._backend.branch_exists(project, branch):
            raise GitLabError(f"branch '{branch}' chưa tồn tại, gọi create_branch trước", 404)
        trailer = f"{IDEMPOTENCY_TRAILER} {idempotency_key}"
        for commit in await self._backend.recent_commits(project, branch):
            if trailer in commit.get("message", ""):
                return {"commit": commit["id"], "created": False}
        actions = [_action(f) for f in files]
        result = await self._backend.commit(
            project, branch, f"{commit_message}\n\n{trailer}", actions
        )
        return {"commit": result["id"], "created": True}

    async def open_merge_request(
        self,
        project: str,
        source_branch: str,
        title: str,
        description: str = "",
        target_branch: str = "main",
    ) -> dict[str, Any]:
        self._c.check_project(project)
        self._c.check_branch(source_branch)
        self._c.check_target(target_branch)
        existing = await self._backend.find_open_mr(project, source_branch)
        if existing is not None:
            return {**_mr_view(existing), "created": False}
        mr = await self._backend.create_mr(
            project, source_branch, target_branch, title, description
        )
        return {**_mr_view(mr), "created": True}

    async def get_merge_request(self, project: str, mr_iid: int) -> dict[str, Any]:
        self._c.check_project(project)
        return _mr_view(await self._backend.get_mr(project, mr_iid))

    async def merge_mr(self, project: str, mr_iid: int, approval_id: str) -> dict[str, Any]:
        self._c.check_project(project)
        if self._verify is None:
            raise ConstraintError("server chưa cấu hình kiểm tra bản ghi duyệt, từ chối merge")
        valid, reason = await self._verify(approval_id, project, mr_iid)
        if not valid:
            raise ConstraintError(f"bản ghi duyệt không hợp lệ: {reason}")
        mr = await self._backend.get_mr(project, mr_iid)
        if mr["state"] == "merged":
            return {**_mr_view(mr), "merged_now": False}
        self._c.check_branch(mr["source_branch"])
        self._c.check_target(mr["target_branch"])
        merged = await self._backend.merge_mr(project, mr_iid)
        return {**_mr_view(merged), "merged_now": True}


def _action(f: dict[str, Any]) -> dict[str, Any]:
    """File của tool push -> action commit; file nhị phân đi kèm encoding = "base64"."""
    action: dict[str, Any] = {"action": f.get("action", "update"), "file_path": f["path"]}
    if action["action"] == "delete":
        return action
    action["content"] = f["content"]
    encoding = f.get("encoding", "text")
    if encoding not in ("text", "base64"):
        raise ValueError(f"encoding '{encoding}' không hợp lệ (text | base64)")
    if encoding == "base64":
        try:
            base64.b64decode(f["content"], validate=True)
        except binascii.Error:
            raise ValueError(f"{f['path']}: content không phải base64") from None
        action["encoding"] = "base64"
    return action
