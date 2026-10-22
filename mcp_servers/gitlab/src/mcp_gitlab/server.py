"""MCP server GitLab. Chạy: `mcp-gitlab` (Streamable HTTP tại /mcp).

Biến môi trường:
- GITLAB_FAKE=1: dùng GitLab giả trong bộ nhớ để chạy thử
- GIT_PROVIDER: gitlab (mặc định) hoặc github; GITHUB_API_URL cho GitHub Enterprise.
  Với GitHub, project là "owner/repo", MR là pull request; token đọc từ GITLAB_TOKEN_FILE.
- GITLAB_URL, GITLAB_TOKEN_FILE (token của bot Developer), GITLAB_MERGE_TOKEN_FILE (merge bot)
- GITLAB_ALLOWED_PROJECTS (phân tách bằng dấu phẩy), GITLAB_BRANCH_PATTERN (mặc định ai/*)
- GITLAB_ALLOWED_TARGETS (mặc định main)
- APPROVAL_VERIFY_URL, APPROVAL_VERIFY_TOKEN_FILE: endpoint của aico xác nhận bản ghi duyệt
- MCP_HOST (mặc định 127.0.0.1), MCP_PORT (mặc định 8101)
"""

from __future__ import annotations

import os
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mcp_gitlab.backend import FakeGitLabBackend, GitLabBackend, GitLabError, HttpGitLabBackend
from mcp_gitlab.github import HttpGitHubBackend
from mcp_gitlab.tools import ApprovalVerifier, ConstraintError, Constraints, GitLabTools


async def _anticipated(call: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    """Lỗi đã lường trước (404, sai ràng buộc...) trả nguyên văn cho agent đọc.

    Exception khác bị MCP SDK che thành "Error executing tool <name>".
    """
    try:
        return await call
    except (GitLabError, ConstraintError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


def build_server(tools: GitLabTools) -> MCPServer:
    server = MCPServer(name="gitlab", instructions="Tool GitLab của AI Company OS.")

    @server.tool(description="Đọc nội dung một file tại ref.")
    async def read_file(project: str, path: str, ref: str = "main") -> dict[str, Any]:
        return await _anticipated(tools.read_file(project, path, ref))

    @server.tool(description="Tải mã nguồn tại ref (tar.gz, base64) cho worker sửa code.")
    async def download_archive(project: str, ref: str = "main") -> dict[str, Any]:
        return await _anticipated(tools.download_archive(project, ref))

    @server.tool(description="Liệt kê issue đang mở có đủ các nhãn.")
    async def list_issues(project: str, labels: list[str]) -> dict[str, Any]:
        return await _anticipated(tools.list_issues(project, labels))

    @server.tool(description="Tạo branch (chỉ branch khớp mẫu cho phép). Idempotent.")
    async def create_branch(project: str, branch: str, ref: str = "main") -> dict[str, Any]:
        return await _anticipated(tools.create_branch(project, branch, ref))

    @server.tool(
        description=(
            "Commit các file lên branch. Idempotent theo idempotency_key. "
            'Mỗi file: {path, action, content}; file nhị phân thêm encoding="base64".'
        )
    )
    async def push(
        project: str,
        branch: str,
        commit_message: str,
        files: list[dict[str, Any]],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await _anticipated(
            tools.push(project, branch, commit_message, files, idempotency_key)
        )

    @server.tool(description="Mở merge request. Idempotent theo source_branch.")
    async def open_merge_request(
        project: str,
        source_branch: str,
        title: str,
        description: str = "",
        target_branch: str = "main",
    ) -> dict[str, Any]:
        return await _anticipated(
            tools.open_merge_request(project, source_branch, title, description, target_branch)
        )

    @server.tool(description="Xem trạng thái merge request.")
    async def get_merge_request(project: str, mr_iid: int) -> dict[str, Any]:
        return await _anticipated(tools.get_merge_request(project, mr_iid))

    @server.tool(description="Merge MR. Chỉ chạy khi approval_id là bản ghi duyệt hợp lệ.")
    async def merge_mr(project: str, mr_iid: int, approval_id: str) -> dict[str, Any]:
        return await _anticipated(tools.merge_mr(project, mr_iid, approval_id))

    return server


def _read_secret(name: str) -> str | None:
    path = os.environ.get(name)
    return Path(path).read_text(encoding="utf-8").strip() if path else None


def _http_verifier(url: str, token: str | None) -> ApprovalVerifier:
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def verify(approval_id: str, project: str, mr_iid: int) -> tuple[bool, str]:
        async with httpx.AsyncClient(timeout=10, headers=headers) as client:
            resp = await client.get(
                f"{url.rstrip('/')}/{approval_id}", params={"project": project, "mr_iid": mr_iid}
            )
        if resp.status_code != 200:
            return False, f"aico trả {resp.status_code}"
        data = resp.json()
        return bool(data.get("valid")), str(data.get("reason", ""))

    return verify


def main() -> None:
    projects = tuple(
        p.strip() for p in os.environ.get("GITLAB_ALLOWED_PROJECTS", "").split(",") if p.strip()
    )
    backend: GitLabBackend
    if os.environ.get("GITLAB_FAKE") == "1":
        # Chạy thử không cần GitLab: dữ liệu nằm trong bộ nhớ, mất khi tắt server.
        fake = FakeGitLabBackend()
        for project in projects:
            fake.project(project).files["main"]["README.md"] = f"# {project}\n"
        backend = fake
    else:
        token = _read_secret("GITLAB_TOKEN_FILE")
        if not token:
            raise SystemExit("thiếu GITLAB_TOKEN_FILE")
        merge_token = _read_secret("GITLAB_MERGE_TOKEN_FILE")
        provider = os.environ.get("GIT_PROVIDER", "gitlab")
        if provider == "github":
            backend = HttpGitHubBackend(
                token,
                merge_token=merge_token,
                api_url=os.environ.get("GITHUB_API_URL") or "https://api.github.com",
            )
        elif provider == "gitlab":
            backend = HttpGitLabBackend(os.environ["GITLAB_URL"], token, merge_token=merge_token)
        else:
            raise SystemExit(f"GIT_PROVIDER không hỗ trợ: {provider}")
    constraints = Constraints(
        allowed_projects=projects,
        branch_pattern=os.environ.get("GITLAB_BRANCH_PATTERN", "ai/*"),
        allowed_targets=tuple(os.environ.get("GITLAB_ALLOWED_TARGETS", "main").split(",")),
    )
    verify_url = os.environ.get("APPROVAL_VERIFY_URL")
    verifier = (
        _http_verifier(verify_url, _read_secret("APPROVAL_VERIFY_TOKEN_FILE"))
        if verify_url
        else None
    )
    server = build_server(GitLabTools(backend, constraints, verifier))
    server.run(
        "streamable-http",
        host=os.environ.get("MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("MCP_PORT", "8101")),
    )
