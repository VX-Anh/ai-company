"""Logic tool GitLab. Server tự kiểm tra repo, branch và bản ghi duyệt, không chỉ dựa vào guard."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

from mcp_gitlab.backend import GitLabBackend, GitLabError

IDEMPOTENCY_TRAILER = "Idempotency-Key:"
MAX_ARCHIVE_BYTES = 25_000_000
# Ký tự tối đa mỗi lần read_file. Tiếng Việt tốn khoảng 1 token mỗi 1,5–2 ký tự; 30.000 ký tự
# nằm dưới mức 25.000 token mà Claude Code chấp nhận cho một kết quả tool MCP.
MAX_READ_CHARS = 30_000

# (approval_id, project, mr_iid) -> (hợp lệ, lý do)
ApprovalVerifier = Callable[[str, str, int], Awaitable[tuple[bool, str]]]


class ConstraintError(PermissionError):
    pass


# Nhánh gốc của ticket chạy lại (Phòng R&D): ai/<x> có base/<x> thì chỉ được vào base/<x>.
BASE_PREFIX = "base/"
_FULL_SHA = re.compile(r"[0-9a-f]{40}")


def base_of(source: str) -> str | None:
    """ai/<x> -> base/<x>; nhánh khác không có nhánh gốc."""
    return BASE_PREFIX + source[3:] if source.startswith("ai/") and len(source) > 3 else None


@dataclass(frozen=True)
class Constraints:
    allowed_projects: tuple[str, ...]
    # Nhiều mẫu phân tách bằng dấu phẩy, vd "ai/*,base/*" (base/*: nhánh gốc của ticket chạy lại).
    branch_pattern: str = "ai/*"
    # Tên nhánh hoặc mẫu fnmatch, vd ("main", "base/*").
    allowed_targets: tuple[str, ...] = ("main",)
    # Đích được MERGE (hẹp hơn đích được mở MR): base/* chỉ để xem diff, không bao giờ merge.
    merge_targets: tuple[str, ...] = ("main",)

    def check_project(self, project: str) -> None:
        if project not in self.allowed_projects:
            raise ConstraintError(f"repo '{project}' không nằm trong danh sách cho phép")

    def check_branch(self, branch: str) -> None:
        bad = not branch or ".." in branch or branch.startswith("/") or branch.endswith("/")
        patterns = [p.strip() for p in self.branch_pattern.split(",") if p.strip()]
        if bad or not any(fnmatchcase(branch, p) for p in patterns):
            raise ConstraintError(f"branch '{branch}' không khớp '{self.branch_pattern}'")

    def check_pair(self, source: str, target: str) -> None:
        """base/<x> (nhánh gốc của ticket chạy lại) chỉ nhận MR từ đúng ai/<x>, không làm nguồn."""
        if source.startswith(BASE_PREFIX):
            raise ConstraintError(f"nhánh gốc '{source}' không được mở MR")
        if target.startswith(BASE_PREFIX) and target != base_of(source):
            raise ConstraintError(f"nhánh '{source}' không được mở MR vào '{target}'")

    def check_push(self, branch: str) -> None:
        self.check_branch(branch)
        if branch.startswith(BASE_PREFIX):
            raise ConstraintError("không push vào base/*")

    def check_merge_target(self, branch: str) -> None:
        if not any(fnmatchcase(branch, t) for t in self.merge_targets):
            raise ConstraintError(f"không được merge vào '{branch}'")

    def check_target(self, branch: str) -> None:
        bad = not branch or ".." in branch
        if bad or not any(fnmatchcase(branch, t) for t in self.allowed_targets):
            raise ConstraintError(f"không được mở MR vào '{branch}'")


def _mr_view(mr: dict[str, Any]) -> dict[str, Any]:
    return {
        "iid": mr["iid"],
        "state": mr["state"],
        "merged": mr["state"] == "merged",
        "source_branch": mr.get("source_branch"),
        "target_branch": mr.get("target_branch"),
        "web_url": mr.get("web_url"),
        # GitHub: base/head có sẵn trong PR; GitLab: diff_refs. Thiếu thì None.
        "base_sha": mr.get("base_sha") or (mr.get("diff_refs") or {}).get("base_sha"),
        "head_sha": mr.get("head_sha") or mr.get("sha"),
        "merge_commit_sha": mr.get("merge_commit_sha"),
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

    async def read_file(
        self,
        project: str,
        path: str,
        ref: str = "main",
        offset: int = 1,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Đọc file theo dòng: offset tính từ 1, limit là số dòng tối đa.

        Mỗi lần trả tối đa MAX_READ_CHARS ký tự: file lớn hơn mà trả nguyên thì CLI của agent
        từ chối kết quả và agent không đọc được gì. Khi bị cắt, `next_offset` cho biết gọi tiếp
        từ dòng nào.
        """
        self._c.check_project(project)
        if offset < 1:
            raise ValueError("offset tính từ 1")
        if limit is not None and limit < 1:
            raise ValueError("limit phải từ 1")
        lines = (await self._backend.get_file(project, path, ref)).splitlines(keepends=True)
        start = min(offset - 1, len(lines))
        stop = len(lines) if limit is None else min(start + limit, len(lines))
        end, size = start, 0
        while end < stop and (end == start or size + len(lines[end]) <= MAX_READ_CHARS):
            size += len(lines[end])
            end += 1
        content = "".join(lines[start:end])
        if len(content) > MAX_READ_CHARS:  # một dòng quá dài: cắt dòng đó
            content = content[:MAX_READ_CHARS]
        result: dict[str, Any] = {
            "path": path,
            "ref": ref,
            "content": content,
            "total_lines": len(lines),
        }
        if start > 0 or end < len(lines):
            result["lines"] = [start + 1, end]
        if end < len(lines):
            result["next_offset"] = end + 1
        if end < stop:  # bị cắt vì giới hạn ký tự, không phải vì limit
            result["truncated"] = True
        return result

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

    async def branch_head(self, project: str, ref: str = "main") -> dict[str, Any]:
        """Commit mới nhất của branch: aico so với lần dựng trước để biết bản chính cần dựng lại."""
        self._c.check_project(project)
        commits = await self._backend.recent_commits(project, ref)
        if not commits:
            raise GitLabError(f"branch {ref} không có commit", 404)
        return {"project": project, "ref": ref, "sha": str(commits[0]["id"])}

    async def merge_base(self, project: str, base: str, head: str) -> dict[str, Any]:
        """Commit chung gần nhất của hai ref: điểm một nhánh tách khỏi nhánh kia."""
        self._c.check_project(project)
        for ref in (base, head):
            # '...' ngăn hai ref trong URL compare của GitHub: ref chứa '..' làm lệch cặp so sánh.
            if not ref or ".." in ref or ref.startswith(("/", "-")):
                raise ValueError(f"ref '{ref}' không hợp lệ")
        sha = await self._backend.merge_base(project, base, head)
        return {"project": project, "base": base, "head": head, "sha": sha}

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
        """Tạo nhánh nếu chưa có. Nhánh base/<x> đánh dấu ai/<x> là nhánh không bao giờ vào main
        (và xoá được bằng delete_branch_pair), nên chỉ tạo được ở một commit cố định (SHA đủ 40 ký
        tự) và trước khi ai/<x> tồn tại: không ai biến nhánh của một ticket đang làm thành cặp đó.
        Quyền tạo base/* do guard quyết định (vai trò hệ thống, không phải agent)."""
        self._c.check_project(project)
        self._c.check_branch(branch)
        if await self._backend.branch_exists(project, branch):
            return {"branch": branch, "created": False}
        if branch.startswith(BASE_PREFIX):
            name = branch[len(BASE_PREFIX) :]
            if not name or "/" in name:
                raise ConstraintError(f"tên nhánh '{branch}' không hợp lệ (base/<id>)")
            if not _FULL_SHA.fullmatch(ref):
                raise ConstraintError(f"'{branch}' phải tạo từ SHA đủ 40 ký tự, không phải '{ref}'")
            if await self._backend.branch_exists(project, f"ai/{name}"):
                raise ConstraintError(f"'ai/{name}' đã tồn tại, không tạo '{branch}'")
        await self._backend.create_branch(project, branch, ref)
        return {"branch": branch, "created": True}

    async def delete_branch_pair(self, project: str, branch: str) -> dict[str, Any]:
        """Xoá cặp nhánh của một ticket chạy lại đã chấm: ai/<x> rồi base/<x>.

        Chỉ nhận tên ai/<x>; nhánh thứ hai luôn là base/<x> suy ra từ đó, nên tool không có cách
        nào trỏ tới main hay một nhánh tuỳ ý. ai/<x> chỉ bị xoá khi base/<x> đang tồn tại (dấu
        hiệu trên repo của ticket chạy lại): nhánh ai/* của ticket thường không có base/<x> nên
        luôn bị từ chối. Xoá ai/<x> trước: nếu xoá base/<x> trước mà bước sau lỗi thì ai/<x> trông
        như nhánh thường và mở được MR vào main. Gọi lại sau lỗi giữa chừng sẽ xoá nốt base/<x>.
        """
        self._c.check_project(project)
        base = base_of(branch)
        if base is None or "/" in branch[3:]:
            raise ConstraintError(f"chỉ xoá được cặp nhánh ai/<id>, không phải '{branch}'")
        self._c.check_branch(branch)
        self._c.check_branch(base)  # server chưa bật mẫu base/* thì không có cặp nào để xoá
        for name in (branch, base):
            if any(fnmatchcase(name, t) for t in (*self._c.merge_targets, "main", "master")):
                raise ConstraintError(f"không bao giờ xoá '{name}'")
        if not await self._backend.branch_exists(project, base):
            if await self._backend.branch_exists(project, branch):
                raise ConstraintError(
                    f"'{branch}' không có nhánh gốc '{base}': là nhánh của ticket thường, không xoá"
                )
            return {"project": project, "deleted": [], "heads": {}}
        open_mr = await self._backend.find_open_mr(project, branch)
        if open_mr is not None and open_mr.get("target_branch") != base:
            raise ConstraintError(
                f"'{branch}' đang có MR mở vào '{open_mr.get('target_branch')}', không xoá"
            )
        deleted: list[str] = []
        heads: dict[str, str] = {}  # SHA trước khi xoá, ghi vào audit để dựng lại khi cần
        for name in (branch, base):
            if not await self._backend.branch_exists(project, name):
                continue
            commits = await self._backend.recent_commits(project, name)
            if commits:
                heads[name] = str(commits[0]["id"])
            await self._backend.delete_branch(project, name)
            deleted.append(name)
        return {"project": project, "deleted": deleted, "heads": heads}

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
        self._c.check_push(branch)
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
        self._c.check_pair(source_branch, target_branch)
        await self._refuse_replay_outside_base(project, source_branch, target_branch)
        existing = await self._backend.find_open_mr(project, source_branch)
        if existing is not None:
            if existing.get("target_branch") not in (None, target_branch):
                raise ConstraintError(
                    f"đã có MR mở từ '{source_branch}' vào '{existing.get('target_branch')}'"
                )
            return {**_mr_view(existing), "created": False}
        mr = await self._backend.create_mr(
            project, source_branch, target_branch, title, description
        )
        return {**_mr_view(mr), "created": True}

    async def _refuse_replay_outside_base(self, project: str, source: str, target: str) -> None:
        """Nhánh có base/<x> là ticket chạy lại (dựng từ commit cũ): chỉ vào base/<x>, không vào
        main dù tên giống ticket thường. Lớp chặn này không dựa vào ticket hay nhãn."""
        base = base_of(source)
        if base and target != base and await self._backend.branch_exists(project, base):
            raise ConstraintError(f"nhánh '{source}' có nhánh gốc '{base}', chỉ được vào đó")

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
        self._c.check_merge_target(mr["target_branch"])
        await self._refuse_replay_outside_base(project, mr["source_branch"], mr["target_branch"])
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
