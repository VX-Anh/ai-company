"""Bản xem trước của MR: aico tải mã nguồn branch qua MCP gitlab, máy build từ xa dựng và phục vụ.

Máy build (deploy/remote/remote_runner.py) chỉ nhận mã nguồn, không có token GitHub. Lỗi dựng
bản xem trước chỉ ghi log, không chặn luồng review/merge.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from aico.core import Artifact
from aico.tickets import TicketStore
from aico.tools import ToolError, ToolGateway

log = logging.getLogger(__name__)

DEFAULT_PREVIEW_BUILD = "npm run build -- --base=./"
# Dòng nhắc agent trong worker: build/test chạy trên server thay vì trong container worker.
REMOTE_BUILD_HINT = (
    "Chạy build/test trên máy build từ xa bằng lệnh `aico-remote '<lệnh>'` (ví dụ "
    "`aico-remote 'npm run build && npm test'`): dependency được cài sẵn theo lockfile, lệnh "
    "chạy không có mạng. Không cài dependency trong thư mục hiện tại. Nếu aico-remote báo máy "
    "build không dùng được, ghi rõ trong phần tóm tắt."
)


def site_name(project: str) -> str:
    """Tên bản chính của repo: phần tên repo, vd VX-Anh/aico-office -> aico-office."""
    return project.rsplit("/", 1)[-1]


class PreviewError(RuntimeError):
    """Không dựng được bản xem trước (tải mã nguồn lỗi, build lỗi, máy build không phản hồi)."""


@dataclass(frozen=True)
class PreviewSettings:
    url: str  # remote runner, ví dụ http://remote-runner:8500
    token: str
    http_client: httpx.AsyncClient
    projects: tuple[str, ...]  # chỉ các repo này được dựng bản xem trước
    build: str = DEFAULT_PREVIEW_BUILD
    output: str = "dist"
    timeout_seconds: float = 900


class RemotePreviewer:
    def __init__(
        self,
        settings: PreviewSettings,
        gateway: ToolGateway,
        tickets: TicketStore,
        role_id: str = "coder",
    ) -> None:
        self._s = settings
        self._gateway = gateway
        self._tickets = tickets
        self._role = role_id
        self._tasks: set[asyncio.Task[None]] = set()

    def enabled_for(self, project: str) -> bool:
        return project in self._s.projects

    async def build(
        self, ticket_id: str, project: str, branch: str, *, site: bool = False
    ) -> dict[str, Any]:
        """Dựng bản xem trước của branch, gắn link vào ticket; trả kết quả của máy build.

        site: dựng bản chính của repo (sau khi merge), tên theo repo (vd aico-office, phục vụ ở
        tên miền trong PREVIEW_HOSTS), không gắn vào ticket.
        """
        if not self.enabled_for(project):
            raise PreviewError(f"repo {project} không nằm trong AICO_PREVIEW_PROJECTS")
        try:
            archive = await self._gateway.call(
                self._role,
                "gitlab",
                "download_archive",
                {"project": project, "ref": branch},
                ticket_id=ticket_id,
            )
        except ToolError as exc:
            raise PreviewError(f"không tải được {branch}: {exc}") from exc
        job = {
            "name": site_name(project) if site else ticket_id,
            "archive": archive["data"],
            "strip": 1,  # archive GitHub/GitLab có một thư mục gốc
            "build": self._s.build,
            "output": self._s.output,
            "timeout_seconds": self._s.timeout_seconds,
            "meta": {"ticket": ticket_id, "project": project, "ref": branch},
        }
        try:
            resp = await self._s.http_client.post(
                f"{self._s.url.rstrip('/')}/preview",
                json=job,
                headers={"Authorization": f"Bearer {self._s.token}"},
                timeout=self._s.timeout_seconds + 120,
            )
            result: dict[str, Any] = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PreviewError(f"máy build không phản hồi: {exc}") from exc
        if resp.status_code != 200 or not result.get("ok"):
            error = result.get("error") or f"HTTP {resp.status_code}"
            raise PreviewError(f"{error}\n{result.get('output', '')}".strip())
        if not site:
            await self._tickets.add_artifact(
                ticket_id, Artifact(kind="preview", ref=branch, url=result["url"]), actor=self._role
            )
        return result

    def schedule(self, ticket_id: str, project: str, branch: str) -> None:
        """Dựng nền sau khi MR có code mới; review không phải chờ."""
        self._spawn(ticket_id, project, branch, site=False)

    def schedule_site(self, ticket_id: str, project: str, ref: str) -> None:
        """Dựng lại bản chính của repo sau khi ticket merge xong."""
        self._spawn(ticket_id, project, ref, site=True)

    def _spawn(self, ticket_id: str, project: str, ref: str, *, site: bool) -> None:
        if not self.enabled_for(project):
            return
        task = asyncio.create_task(self._build_logged(ticket_id, project, ref, site))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _build_logged(self, ticket_id: str, project: str, branch: str, site: bool) -> None:
        try:
            result = await self.build(ticket_id, project, branch, site=site)
            log.info("ticket %s: bản xem trước %s", ticket_id, result.get("url"))
        except PreviewError as exc:
            log.warning("ticket %s: không dựng được bản xem trước: %s", ticket_id, exc)
        except Exception:
            log.exception("ticket %s: lỗi khi dựng bản xem trước", ticket_id)
