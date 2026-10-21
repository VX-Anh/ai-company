"""Xử lý ticket đã escalate: người duyệt cho merge MR hiện có, merge_bot merge qua guard + audit.

Dùng khi luồng tự động đã dừng (ví dụ review hết vòng) nhưng người đã tự kiểm MR và muốn merge.
Vẫn ghi bản ghi duyệt merge như luồng thường, nên mcp-gitlab tự kiểm được trước khi merge.
"""

from __future__ import annotations

from aico.core import ApprovalKind, Decision, Ticket, TicketStatus
from aico.governance import ApprovalStore
from aico.tickets import TicketStore
from aico.tools import ApprovalRequired, ToolDenied, ToolError, ToolGateway

S = TicketStatus
# Vòng riêng cho duyệt merge sau escalate, không đụng các vòng duyệt của graph (1, 2, ...).
ESCALATION_MERGE_ROUND = 999


class EscalationError(ValueError):
    """Ticket không merge được theo cách này (sai trạng thái, không có MR, merge lỗi)."""


async def merge_escalated(
    tickets: TicketStore,
    approvals: ApprovalStore,
    gateway: ToolGateway,
    ticket_id: str,
    *,
    actor: str,
    comment: str = "",
    merge_role: str = "merge_bot",
) -> Ticket:
    ticket = await tickets.get(ticket_id)
    if ticket.status is not S.ESCALATED:
        raise EscalationError(f"ticket đang '{ticket.status}', không ở trạng thái escalated")
    mrs = [a for a in ticket.artifacts if a.kind == "merge_request"]
    if not mrs:
        raise EscalationError("ticket chưa có merge request")
    project, _, iid = mrs[-1].ref.rpartition("!")
    approval = await approvals.record(
        ticket_id,
        ApprovalKind.MERGE,
        ESCALATION_MERGE_ROUND,
        Decision.APPROVED,
        actor=actor,
        comment=comment,
    )
    if not approval.is_approved:
        raise EscalationError(f"đã có quyết định '{approval.decision}' cho lần duyệt này")
    args = {"project": project, "mr_iid": int(iid)}
    try:
        await gateway.call(
            merge_role,
            "gitlab",
            "merge_mr",
            {**args, "approval_id": approval.id},
            ticket_id=ticket_id,
        )
        mr = await gateway.call(
            merge_role, "gitlab", "get_merge_request", args, ticket_id=ticket_id
        )
    except (ToolError, ToolDenied, ApprovalRequired) as exc:
        raise EscalationError(f"merge lỗi: {exc}") from exc
    if not mr.get("merged"):
        raise EscalationError(f"MR ở trạng thái '{mr.get('state')}' sau khi merge")
    return await tickets.transition(
        ticket_id,
        S.DONE,
        actor=actor,
        data={"merged_by": merge_role, "approval_id": approval.id, "comment": comment},
    )
