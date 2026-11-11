"""Librarian (vai trò hệ thống, không gọi model): dựng lại chỉ mục khi main của kho tri thức đổi.

Theo dõi bằng branch_head định kỳ nên bắt được cả merge qua aico lẫn merge tay trên GitHub.
Archive tải qua gateway (guard + audit) bằng vai trò `librarian`.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

from aico.rnd.cards import cards_from_archive
from aico.rnd.store import MAIN, KnowledgeStore
from aico.tools import ToolGateway

log = logging.getLogger(__name__)

LIBRARIAN_ROLE = "librarian"


class Librarian:
    def __init__(
        self,
        store: KnowledgeStore,
        gateway: ToolGateway,
        project: str,
        *,
        branch: str = MAIN,
        role_id: str = LIBRARIAN_ROLE,
    ) -> None:
        self._store = store
        self._gateway = gateway
        self._project = project
        self._branch = branch
        self._role = role_id

    async def _call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return await self._gateway.call(
            self._role, "gitlab", tool, {"project": self._project, **args}, ticket_id=None
        )

    async def check_once(self, *, force: bool = False) -> int | None:
        """Dựng lại nếu main có commit mới; trả số thẻ đã nạp, hoặc None khi không cần dựng."""
        head = await self._call("branch_head", {"ref": self._branch})
        sha = str(head.get("sha") or "")
        if not sha:
            return None
        if not force and await self._store.last_commit(MAIN) == sha:
            return None
        # Tải theo sha, không theo tên branch: tránh bản cũ trong cache tarball của GitHub.
        archive = await self._call("download_archive", {"ref": sha})
        cards, skipped = cards_from_archive(base64.b64decode(str(archive["data"])))
        for reason in skipped:
            log.warning("bỏ qua thẻ: %s", reason)
        count = await self._store.rebuild(cards, ref=MAIN, commit=sha, skipped=skipped)
        log.info("dựng chỉ mục tri thức %s@%s: %d thẻ", self._project, sha[:7], count)
        return count

    async def run_forever(self, interval_seconds: float) -> None:
        while True:
            try:
                await self.check_once()
            except Exception:  # mạng/GitHub lỗi một lượt: thử lại lượt sau
                log.exception("lỗi khi dựng chỉ mục tri thức %s", self._project)
            await asyncio.sleep(interval_seconds)
