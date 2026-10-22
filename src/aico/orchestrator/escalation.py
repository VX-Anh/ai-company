"""Xử lý ticket đã escalate khi luồng tự động đã dừng.

- merge: người đã tự kiểm MR và cho merge; merge_bot merge qua guard + audit, vẫn ghi bản ghi duyệt
  merge như luồng thường nên mcp-gitlab tự kiểm được trước khi merge.
- retry_push: bước code đã viết xong nhưng đẩy lên Git lỗi; đẩy lại phần thay đổi lưu trong event
  escalated, mở MR. Ticket vẫn escalated để người xem MR rồi chọn merge hoặc đóng.
"""

from __future__ import annotations

from typing import Any

from aico.core import ApprovalKind, Artifact, Decision, Ticket, TicketStatus
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


async def _file_action(
    gateway: ToolGateway, role: str, ticket_id: str, project: str, ref: str, path: str
) -> str:
    """create | update theo việc file đã có ở branch gốc (GitLab phân biệt, GitHub thì không)."""
    try:
        await gateway.call(
            role,
            "gitlab",
            "read_file",
            {"project": project, "path": path, "ref": ref},
            ticket_id=ticket_id,
        )
    except ToolError:
        return "create"
    return "update"


async def retry_push(
    tickets: TicketStore,
    gateway: ToolGateway,
    ticket_id: str,
    *,
    actor: str,
    comment: str = "",
    code_role: str = "coder",
    default_project: str | None = None,
    target_branch: str = "main",
) -> Ticket:
    """Đẩy lại code chưa đẩy được (event escalated gần nhất) lên ai/<ticket> và mở MR."""
    ticket = await tickets.get(ticket_id)
    if ticket.status is not S.ESCALATED:
        raise EscalationError(f"ticket đang '{ticket.status}', không ở trạng thái escalated")
    escalated = [e for e in await tickets.events(ticket_id) if e.to_status is S.ESCALATED]
    last = escalated[-1] if escalated else None
    changes: list[dict[str, Any]] = (last.data.get("unpushed_changes") or []) if last else []
    if last is None or not changes:
        raise EscalationError("lần escalate gần nhất không có code chờ đẩy lại")
    project = ticket.project or default_project
    if not project:
        raise EscalationError("ticket không có repo")
    branch = f"ai/{ticket_id}"
    # Cùng một lần escalate thì cùng khoá: bấm lại không tạo commit trùng.
    key = f"{ticket_id}:retry:{last.at.isoformat()}"
    note = f" ({comment})" if comment else ""
    try:
        files: list[dict[str, Any]] = []
        for c in changes:
            f: dict[str, Any] = {"path": c["path"], "action": c.get("action", "update")}
            if f["action"] != "delete":
                f["content"] = c.get("content", "")
                if c.get("encoding") == "base64":
                    f["encoding"] = "base64"
                f["action"] = await _file_action(
                    gateway, code_role, ticket_id, project, target_branch, c["path"]
                )
            files.append(f)
        await gateway.call(
            code_role,
            "gitlab",
            "create_branch",
            {"project": project, "branch": branch, "ref": target_branch},
            ticket_id=ticket_id,
        )
        await gateway.call(
            code_role,
            "gitlab",
            "push",
            {
                "project": project,
                "branch": branch,
                "commit_message": f"{ticket_id}: đẩy lại thay đổi chưa đẩy được{note}",
                "files": files,
                "idempotency_key": key,
            },
            ticket_id=ticket_id,
        )
        mr = await gateway.call(
            code_role,
            "gitlab",
            "open_merge_request",
            {
                "project": project,
                "source_branch": branch,
                "target_branch": target_branch,
                "title": f"{ticket_id}: {ticket.title}",
                "description": f"Đẩy lại bởi {actor} sau khi bước code đẩy lên Git lỗi.{note}",
            },
            ticket_id=ticket_id,
        )
    except (ToolError, ToolDenied, ApprovalRequired) as exc:
        raise EscalationError(f"đẩy lại lỗi: {exc}") from exc
    await tickets.add_artifact(ticket_id, Artifact(kind="branch", ref=branch), actor=actor)
    return await tickets.add_artifact(
        ticket_id,
        Artifact(kind="merge_request", ref=f"{project}!{mr['iid']}", url=mr.get("web_url")),
        actor=actor,
    )
