"""Bản xem trước của MR: aico tải mã nguồn branch qua MCP gitlab, máy build từ xa dựng và phục vụ.

Máy build (deploy/remote/remote_runner.py) chỉ nhận mã nguồn, không có token GitHub. Lỗi dựng
bản xem trước chỉ ghi log, không chặn luồng review/merge.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
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
# Reviewer của repo có giao diện tự xem giao diện thật, không chỉ đọc diff (3 ticket GUI gần đây
# reviewer bắt lỗi logic nhưng không thấy cảnh bị nhỏ hay chữ mất dấu).
SHOT_HINT = (
    "Repo này có giao diện: sau build/test, tự xem giao diện bằng `aico-shot` (dựng bản xem "
    "trước của thư mục hiện tại trên máy build, chụp bằng Chromium headless ở 1440x900 và "
    "390x844, ảnh PNG lưu trong .aico-shots/). Thêm `--query` cho trạng thái cần xem, vd "
    "`aico-shot --query hour=11 --query hour=21`; `aico-shot --help` xem tuỳ chọn. Mở từng PNG "
    "bằng tool đọc file/xem ảnh của bạn (Codex: view_image) và kiểm tra phần giao diện ticket "
    "yêu cầu có hiện đúng không, có vỡ bố cục, chồng chữ, chữ mất dấu tiếng Việt không; đọc cả "
    "lỗi console aico-shot in ra. Ghi trong nhận xét đã xem ảnh nào và thấy gì. aico-shot lỗi "
    "hoặc không xem được ảnh thì ghi rõ, riêng điều đó không phải lý do CẦN SỬA. Ảnh trong "
    ".aico-shots/ (tối đa 16) được gắn vào ticket cho người duyệt xem: chụp đúng trạng thái "
    "liên quan tới ticket."
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
        self, ticket_id: str | None, project: str, branch: str, *, site: bool = False
    ) -> dict[str, Any]:
        """Dựng bản xem trước của branch, gắn link vào ticket; trả kết quả của máy build.

        site: dựng bản chính của repo (sau khi merge), tên theo repo (vd aico-office, phục vụ ở
        tên miền trong PREVIEW_HOSTS), không gắn vào ticket.
        """
        if not self.enabled_for(project):
            raise PreviewError(f"repo {project} không nằm trong AICO_PREVIEW_PROJECTS")
        if ticket_id is None and not site:
            raise PreviewError("bản xem trước của branch cần ticket")
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
            "name": site_name(project) if site or ticket_id is None else ticket_id,
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
        if not site and ticket_id is not None:
            await self._tickets.add_artifact(
                ticket_id, Artifact(kind="preview", ref=branch, url=result["url"]), actor=self._role
            )
        return result

    async def publish_shots(self, ticket_id: str, shots: list[dict[str, Any]]) -> None:
        """Đưa ảnh review lên máy build (phục vụ cạnh bản xem trước), gắn artifact screenshot.

        Mỗi lần review một thư mục riêng theo thời điểm; GUI hiện lần mới nhất (ref sắp theo
        thời điểm), lần cũ vẫn xem được.
        """
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        files = [
            {"name": str(s.get("name", "")), "data": str(s.get("data", ""))}
            for s in shots
            if isinstance(s, dict)
        ]
        try:
            resp = await self._s.http_client.post(
                f"{self._s.url.rstrip('/')}/shots",
                json={"name": f"img-{ticket_id}-{stamp}", "files": files},
                headers={"Authorization": f"Bearer {self._s.token}"},
                timeout=120,
            )
            result: dict[str, Any] = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PreviewError(f"máy build không nhận ảnh: {exc}") from exc
        if resp.status_code != 200 or not result.get("ok"):
            raise PreviewError(str(result.get("error") or f"HTTP {resp.status_code}"))
        for item in result.get("files", []):
            await self._tickets.add_artifact(
                ticket_id,
                Artifact(kind="screenshot", ref=f"{stamp}/{item['name']}", url=item["url"]),
                actor="reviewer",
            )

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


class SiteWatcher:
    """Dựng lại bản chính khi branch đích có commit mới, kể cả commit không đi qua aico
    (merge tay trên GitHub, push thẳng). Lần đầu thấy một repo thì dựng luôn để bản đang
    phục vụ khớp branch sau khi aico khởi động lại.
    """

    def __init__(
        self,
        previewer: RemotePreviewer,
        gateway: ToolGateway,
        projects: tuple[str, ...],
        *,
        branch: str = "main",
        role_id: str = "coder",
        retry_seconds: float = 300,
        max_retry_seconds: float = 3600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._previewer = previewer
        self._gateway = gateway
        self._projects = projects
        self._branch = branch
        self._role = role_id
        self._retry = retry_seconds
        self._max_retry = max_retry_seconds
        self._clock = clock
        # sha đã dựng thành công: không dựng lại tới khi branch có commit mới.
        self._seen: dict[str, str] = {}
        # Build lỗi (máy build tắt, mất SSH): thử lại cùng sha sau retry_seconds, gấp đôi mỗi lần
        # lỗi, tối đa max_retry_seconds, để không gọi SSH hỏng mỗi phút. project -> (sha, số lần
        # lỗi, thời điểm được thử lại).
        self._failed: dict[str, tuple[str, int, float]] = {}

    async def check_once(self) -> list[str]:
        """Một lượt kiểm tra; trả các repo đã dựng lại thành công."""
        built: list[str] = []
        for project in self._projects:
            try:
                head = await self._gateway.call(
                    self._role,
                    "gitlab",
                    "branch_head",
                    {"project": project, "ref": self._branch},
                    ticket_id=None,
                )
            except Exception as exc:  # mạng/GitHub lỗi một lượt: thử lại lượt sau
                log.warning("không đọc được %s@%s: %s", project, self._branch, exc)
                continue
            sha = str(head.get("sha") or "")
            if not sha or self._seen.get(project) == sha:
                continue
            failed = self._failed.get(project)
            attempts = 0
            if failed is not None and failed[0] == sha:
                attempts = failed[1]
                if self._clock() < failed[2]:
                    continue  # chưa tới lúc thử lại
            try:
                result = await self._previewer.build(None, project, self._branch, site=True)
            except PreviewError as exc:
                attempts += 1
                wait = min(self._retry * 2 ** (attempts - 1), self._max_retry)
                self._failed[project] = (sha, attempts, self._clock() + wait)
                log.warning(
                    "không dựng được bản chính %s@%s (lần %d, thử lại sau %d giây): %s",
                    project,
                    sha[:7],
                    attempts,
                    wait,
                    exc,
                )
                continue
            self._seen[project] = sha
            self._failed.pop(project, None)
            log.info("dựng lại bản chính %s@%s: %s", project, sha[:7], result.get("url"))
            built.append(project)
        return built

    async def run_forever(self, interval_seconds: float) -> None:
        while True:
            try:
                await self.check_once()
            except Exception:
                log.exception("lỗi khi theo dõi branch %s", self._branch)
            await asyncio.sleep(interval_seconds)
