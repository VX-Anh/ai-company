from __future__ import annotations

from typing import Any, TypedDict


class TicketState(TypedDict):
    """Node đọc khóa đã được node trước ghi; input đầu tiên chỉ có ticket_id."""

    ticket_id: str
    risk: str
    requirements: str  # yêu cầu + tiêu chí nghiệm thu do BA viết; rỗng nếu bỏ qua bước phân tích
    plan: str
    work_items: list[dict[str, Any]]  # đầu việc đọc từ kế hoạch (WorkItem.to_dict)
    architecture: str  # sơ đồ Mermaid Tech Lead cập nhật, ghi vào docs/architecture.md
    plan_notes: list[str]
    plan_rounds: int
    plan_decision: str
    plan_feedback: str
    code_attempts: int
    branch: str
    mr_iid: int
    review_rounds: int
    review_passed: bool
    review_notes: str
    skip_test: bool  # ticket mang nhãn bỏ bước test (vd docs)
    test_rounds: int
    test_passed: bool
    test_notes: str
    merge_approval_rounds: int
    merge_decision: str
    merge_approval_id: str
    merge_attempts: int
    merge_error: str
    merged: bool
    escalation_reason: str
    unpushed_changes: list[dict[str, Any]]  # code chưa đẩy được lên Git (giữ lại khi escalate)
