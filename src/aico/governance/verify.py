"""Xác nhận bản ghi duyệt merge cho MCP server (server tự kiểm tra, không tin guard)."""

from __future__ import annotations

from aico.core import ApprovalKind
from aico.governance.approvals import ApprovalStore
from aico.tickets import TicketNotFound, TicketStore


async def verify_merge_approval(
    approvals: ApprovalStore,
    tickets: TicketStore,
    approval_id: str,
    project: str,
    mr_iid: int,
) -> tuple[bool, str]:
    approval = await approvals.get(approval_id)
    if approval is None:
        return False, "không có bản ghi duyệt"
    if approval.kind is not ApprovalKind.MERGE:
        return False, "không phải bản ghi duyệt merge"
    if not approval.is_approved:
        return False, f"quyết định là '{approval.decision}'"
    try:
        ticket = await tickets.get(approval.ticket_id)
    except TicketNotFound:
        return False, "ticket không tồn tại"
    ref = f"{project}!{mr_iid}"
    if not any(a.kind == "merge_request" and a.ref == ref for a in ticket.artifacts):
        return False, f"MR {ref} không thuộc ticket {ticket.id}"
    return True, "ok"
