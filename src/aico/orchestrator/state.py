from __future__ import annotations

from typing import TypedDict


class TicketState(TypedDict):
    """Node đọc khóa đã được node trước ghi; input đầu tiên chỉ có ticket_id."""

    ticket_id: str
    risk: str
    plan: str
    plan_rounds: int
    plan_decision: str
    plan_feedback: str
    code_attempts: int
    branch: str
    mr_iid: int
    review_rounds: int
    review_passed: bool
    review_notes: str
    merge_approval_rounds: int
    merge_decision: str
    merge_approval_id: str
    merge_attempts: int
    merge_error: str
    merged: bool
    escalation_reason: str
