"""Lấy ticket mới bằng polling GitLab (máy cá nhân không nhận được webhook)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence

from aico.core import Ticket, TicketSource
from aico.tickets import TicketStore
from aico.tools import ToolGateway

log = logging.getLogger(__name__)


def new_ticket_id() -> str:
    return f"T-{uuid.uuid4().hex[:8]}"


class GitLabIssuePoller:
    """Issue đang mở có nhãn `label` thành ticket. Idempotent theo source_ref."""

    def __init__(
        self,
        gateway: ToolGateway,
        tickets: TicketStore,
        projects: Sequence[str],
        *,
        label: str = "ai-company",
        role: str = "intake",
        budget_usd: float = 5.0,
    ) -> None:
        self._gateway = gateway
        self._tickets = tickets
        self._projects = tuple(projects)
        self._label = label
        self._role = role
        self._budget = budget_usd

    async def poll_once(self) -> list[Ticket]:
        created: list[Ticket] = []
        for project in self._projects:
            result = await self._gateway.call(
                self._role,
                "gitlab",
                "list_issues",
                {"project": project, "labels": [self._label]},
                ticket_id=None,
            )
            for issue in result.get("issues", []):
                ticket = Ticket(
                    id=new_ticket_id(),
                    title=issue["title"],
                    body=issue.get("description", ""),
                    source=TicketSource.GITLAB,
                    source_ref=f"gitlab:{project}#{issue['iid']}",
                    project=project,
                    labels=[lbl for lbl in issue.get("labels", []) if lbl != self._label],
                    budget_usd=self._budget,
                )
                saved, is_new = await self._tickets.create_from_source(ticket, actor=self._role)
                if is_new:
                    created.append(saved)
        return created

    async def run_forever(self, interval_seconds: float = 60) -> None:
        while True:
            try:
                for t in await self.poll_once():
                    log.info("ticket mới %s từ %s", t.id, t.source_ref)
            except Exception:
                log.exception("polling GitLab lỗi")
            await asyncio.sleep(interval_seconds)
