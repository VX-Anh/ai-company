"""Bộ lập lịch dò công nghệ của Phòng R&D (R2): tạo ticket `scan` khi tới kỳ và hệ đang rảnh.

Mặc định TẮT (`AICO_RND_SCAN_INTERVAL_DAYS=0`). Bật thì mỗi giờ kiểm một lần, chỉ tạo ticket khi:
- đã quá `interval_days` kể từ ticket dò gần nhất (nhãn `scan`);
- không có ticket nào đang chạy (trạng thái làm việc) và không agent nào đang chạy;
- mọi phòng có công bố hạn mức đều dưới ngưỡng 5 giờ và tuần (hạn mức để dành cho ticket thật).
Quyết định nằm trong hàm thuần `should_scan` để test từng điều kiện.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aico.core import Ticket, TicketStatus
from aico.intake import new_ticket_id
from aico.tickets import TicketStore

log = logging.getLogger(__name__)

SCAN_LABEL = "scan"
ACTIVE = frozenset(
    {
        TicketStatus.NEW,
        TicketStatus.TRIAGED,
        TicketStatus.PLANNED,
        TicketStatus.IN_PROGRESS,
        TicketStatus.IN_REVIEW,
        TicketStatus.IN_TESTING,
        TicketStatus.MERGING,
    }
)


@dataclass(frozen=True)
class ScanPolicy:
    interval_days: int = 0  # 0: tắt
    max_session_percent: float = 50  # cửa sổ ngắn (5 giờ / session)
    max_week_percent: float = 60


def _window_ok(window: Mapping[str, Any], policy: ScanPolicy) -> bool:
    minutes = float(window.get("window_minutes") or 0)
    used = float(window.get("used_percent") or 0)
    limit = policy.max_week_percent if minutes >= 24 * 60 else policy.max_session_percent
    return used < limit


def should_scan(
    *,
    now: datetime,
    policy: ScanPolicy,
    last_scan_at: datetime | None,
    active_tickets: int,
    rooms: Iterable[Mapping[str, Any]],
) -> tuple[bool, str]:
    """(có tạo ticket dò không, lý do). Lý do ghi log để owner biết vì sao chưa chạy."""
    if policy.interval_days <= 0:
        return False, "tắt"
    if last_scan_at is not None and now - last_scan_at < timedelta(days=policy.interval_days):
        return False, "chưa tới kỳ"
    if active_tickets:
        return False, f"đang có {active_tickets} ticket chạy"
    for room in rooms:
        if room.get("agents"):
            return False, f"phòng {room.get('name')} đang có agent"
        if room.get("state") in ("limited", "offline"):
            continue  # phòng hết hạn mức hoặc tắt không nhận việc dò, không chặn phòng khác
        windows = (room.get("usage") or {}).get("windows") or []
        if not all(_window_ok(w, policy) for w in windows):
            return False, f"phòng {room.get('name')} đã dùng nhiều hạn mức"
    return True, "tới kỳ, hệ rảnh"


class ScanScheduler:
    def __init__(
        self,
        tickets: TicketStore,
        make_ticket: Callable[[int], dict[str, Any]],
        rooms: Callable[[], Awaitable[list[dict[str, Any]]]],
        policy: ScanPolicy,
        clock: Callable[[], datetime],
    ) -> None:
        self._tickets = tickets
        self._make = make_ticket
        self._rooms = rooms
        self._policy = policy
        self._clock = clock

    async def tick(self) -> Ticket | None:
        now = self._clock()
        all_tickets = await self._tickets.list()
        scans = [t for t in all_tickets if SCAN_LABEL in {x.lower() for x in t.labels}]
        last = max((t.created_at for t in scans), default=None)
        active = sum(1 for t in all_tickets if t.status in ACTIVE)
        ok, reason = should_scan(
            now=now,
            policy=self._policy,
            last_scan_at=last,
            active_tickets=active,
            rooms=await self._rooms(),
        )
        if not ok:
            log.info("dò công nghệ: chưa chạy (%s)", reason)
            return None
        data = self._make(self._policy.interval_days)
        ticket = Ticket(
            id=new_ticket_id(),
            title=data["title"],
            body=data["body"],
            project=data["project"],
            labels=list(data["labels"]),
        )
        created = await self._tickets.create(ticket, actor="rnd-scheduler")
        log.info("dò công nghệ: tạo %s", created.id)
        return created

    async def run_forever(self, every_seconds: float = 3600) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("bộ lập lịch dò công nghệ lỗi")
            await asyncio.sleep(every_seconds)
