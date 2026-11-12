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
    item_results: list[dict[str, Any]]  # bảng kết quả đầu việc của lần code gần nhất (doc_row)
    gate_rounds: int  # số lần liên tiếp cổng trước review trả về bước code
    gate_passed: bool
    gate_feedback: str  # góp ý của cổng cho lần code kế; rỗng khi cổng đã cho qua
    gate_retry_items: list[str]  # đầu việc chưa xong cần chạy lại ở lần code kế
    gate_retried: list[str]  # đầu việc cổng đã cho chạy lại ít nhất một lần
    # File (đổi hoặc xoá) đã có trên nhánh ở lần gần nhất nhánh được gửi review: cổng chỉ xét
    # phần mới. Thiếu khoá (ticket có từ trước khi có cổng): lần kiểm đầu nhận hết diff hiện có.
    gate_accepted: list[str]
    gate_feedback_log: str  # mọi góp ý (review, kiểm thử, người duyệt, merge lỗi) ticket từng nhận
    review_rounds: int
    review_passed: bool
    review_notes: str
    skip_test: bool  # ticket mang nhãn bỏ bước test (vd docs)
    max_review_rounds: int  # giới hạn vòng review của ticket (theo nhãn hoặc SOP)
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
    eval: bool  # ticket chạy lại của R&D (nhãn + artifact eval): bỏ duyệt kế hoạch, không merge
    unpushed_changes: list[dict[str, Any]]  # code chưa đẩy được lên Git (giữ lại khi escalate)
