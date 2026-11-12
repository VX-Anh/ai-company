"""Cổng hạn mức của instance chạy lại (Phòng R&D, R1): khi nào được bắt đầu một ticket mới.

Ticket chạy lại dùng chung subscription với ticket thật, nên instance `aico-eval` chỉ nhận ticket
MỚI (trạng thái new) khi:
- chưa chạm trần số ticket chạy lại bắt đầu trong 7 ngày gần nhất (`AICO_EVAL_WEEKLY_CAP`);
- hệ thật đang rảnh: không ticket thường nào đang chạy hay chờ runner xử lý quyết định;
- không phòng nào có agent và mọi cửa sổ hạn mức đều dưới ngưỡng (`rooms_free`, dùng chung với
  bộ lập lịch dò công nghệ).
Ticket đã bắt đầu (đang dở, hoặc có quyết định chờ xử lý như `--close`) vẫn được nhận: cổng không
bỏ dở việc. Quyết định nằm trong hàm thuần `should_start`; lý do chưa chạy được ghi log.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aico.core import TicketStatus
from aico.orchestrator.workplan import eval_spec
from aico.rnd.scheduler import ACTIVE, rooms_free
from aico.tickets import TicketStore

log = logging.getLogger(__name__)

WINDOW = timedelta(days=7)


@dataclass(frozen=True)
class EvalPolicy:
    weekly_cap: int = 40  # số ticket chạy lại được bắt đầu mỗi 7 ngày (mọi biến thể); 0: không chạy
    max_session_percent: float = 70  # cửa sổ ngắn (5 giờ / session)
    max_week_percent: float = 60


def should_start(
    *,
    policy: EvalPolicy,
    pending_new: int,
    started_last_week: int,
    active_tickets: int,
    rooms: Iterable[Mapping[str, Any]] | None,
) -> tuple[bool, str]:
    """(có nhận ticket chạy lại mới không, lý do). `rooms=None`: không đọc được hạn mức các phòng
    (không có worker CLI, chỉ khi chạy thử với agent giả) thì bỏ qua điều kiện phòng."""
    if not pending_new:
        return False, "không có ticket mới"
    if started_last_week >= policy.weekly_cap:
        return False, f"đã chạy {started_last_week}/{policy.weekly_cap} ticket trong 7 ngày"
    if active_tickets:
        return False, f"hệ thật đang có {active_tickets} ticket chạy"
    if rooms is not None:
        free, reason = rooms_free(
            rooms,
            max_session_percent=policy.max_session_percent,
            max_week_percent=policy.max_week_percent,
        )
        if not free:
            return False, reason
    return True, "hệ rảnh, còn hạn mức"


class EvalGate:
    """Cổng cho Runner của instance chạy lại: `await gate()` trả True khi được nhận ticket mới.

    Kết quả "chưa" được giữ `recheck_seconds` (runner hỏi mỗi vài giây khi rảnh; mỗi lần kiểm phải
    đọc danh sách ticket và trạng thái các phòng). Kết quả "được" không giữ: mỗi ticket mới đều
    qua kiểm tra, nên không vượt trần. Kiểm tra lỗi thì coi như chưa được (an toàn cho hạn mức).
    """

    def __init__(
        self,
        tickets: TicketStore,
        rooms: Callable[[], Awaitable[list[dict[str, Any]]]] | None,
        policy: EvalPolicy,
        clock: Callable[[], datetime],
        *,
        variant: str | None = None,
        recheck_seconds: float = 60,
    ) -> None:
        self._tickets = tickets
        self._rooms = rooms
        self._policy = policy
        self._clock = clock
        self._variant = variant
        self._recheck = timedelta(seconds=recheck_seconds)
        self._closed_until: datetime | None = None
        self.last_reason = ""

    async def _started_at(self, ticket_id: str) -> datetime | None:
        """Lúc ticket rời trạng thái new (bắt đầu tốn hạn mức)."""
        for event in await self._tickets.events(ticket_id):
            if event.kind == "status" and event.from_status is TicketStatus.NEW:
                return event.at
        return None

    async def check(self) -> tuple[bool, str]:
        now = self._clock()
        since = now - WINDOW
        pending_new = started = active = 0
        for ticket in await self._tickets.list():
            spec = eval_spec(ticket)
            if spec is None:
                if ticket.status in ACTIVE or ticket.pending_resume:
                    active += 1
            elif ticket.status is TicketStatus.NEW:
                if self._variant is None or spec[0] == self._variant:
                    pending_new += 1
            elif ticket.updated_at >= since:  # bắt đầu trong 7 ngày thì cũng cập nhật trong 7 ngày
                at = await self._started_at(ticket.id)
                if at is not None and at >= since:
                    started += 1
        # Không có ticket mới thì khỏi hỏi trạng thái các phòng (gọi HTTP tới từng worker).
        rooms = await self._rooms() if self._rooms is not None and pending_new else None
        return should_start(
            policy=self._policy,
            pending_new=pending_new,
            started_last_week=started,
            active_tickets=active,
            rooms=rooms,
        )

    async def __call__(self) -> bool:
        now = self._clock()
        if self._closed_until is not None and now < self._closed_until:
            return False
        try:
            ok, reason = await self.check()
        except Exception as exc:
            log.exception("cổng hạn mức chạy lại lỗi")
            ok, reason = False, f"kiểm tra lỗi: {type(exc).__name__}"
        if ok:
            self._closed_until = None
            log.info("chạy lại: nhận ticket mới (%s)", reason)
        else:
            self._closed_until = now + self._recheck
            # Lý do đổi mới ghi log: runner hỏi liên tục, không lặp cùng một dòng mỗi phút.
            if reason != self.last_reason:
                log.info("chạy lại: chưa nhận ticket mới (%s)", reason)
        self.last_reason = reason
        return ok
