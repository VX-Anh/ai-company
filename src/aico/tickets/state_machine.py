"""Bảng chuyển trạng thái ticket (mục Ticket service trong thiết kế)."""

from __future__ import annotations

from aico.core import TERMINAL_STATUSES, TicketStatus

S = TicketStatus

TRANSITIONS: dict[TicketStatus, frozenset[TicketStatus]] = {
    S.NEW: frozenset({S.TRIAGED}),
    S.TRIAGED: frozenset({S.PLANNED}),
    S.PLANNED: frozenset({S.AWAITING_PLAN_APPROVAL, S.IN_PROGRESS, S.ESCALATED}),
    S.AWAITING_PLAN_APPROVAL: frozenset({S.IN_PROGRESS, S.PLANNED, S.ESCALATED}),
    # Hết ngân sách giữa chừng cũng phải dừng và hỏi người.
    S.IN_PROGRESS: frozenset({S.IN_REVIEW, S.ESCALATED}),
    S.IN_REVIEW: frozenset({S.AWAITING_MERGE_APPROVAL, S.IN_PROGRESS, S.ESCALATED}),
    S.AWAITING_MERGE_APPROVAL: frozenset({S.MERGING, S.IN_PROGRESS, S.ESCALATED}),
    S.MERGING: frozenset({S.DONE, S.IN_PROGRESS, S.ESCALATED}),
    # Người nhận ticket escalate có thể xử lý tay, cho chạy lại hoặc đóng.
    S.ESCALATED: frozenset({S.PLANNED, S.IN_PROGRESS, S.DONE}),
    S.DONE: frozenset(),
    S.FAILED: frozenset(),
}


class TransitionError(ValueError):
    pass


def can_transition(current: TicketStatus, target: TicketStatus) -> bool:
    if current == target:
        # Node chạy lại sau restart: chuyển sang chính trạng thái hiện tại là no-op.
        return True
    if target is S.FAILED:
        return current not in TERMINAL_STATUSES
    return target in TRANSITIONS[current]


def check_transition(current: TicketStatus, target: TicketStatus) -> None:
    if not can_transition(current, target):
        raise TransitionError(f"không được chuyển ticket từ '{current}' sang '{target}'")
