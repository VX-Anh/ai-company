"""REST API cho người dùng và console: tạo ticket, xem trạng thái, gửi quyết định duyệt.

Chỉ bind 127.0.0.1. Endpoint /internal chỉ dành cho MCP server, cần token nội bộ.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from aico.core import Decision, Event, Ticket, TicketStatus
from aico.governance import ApprovalStore
from aico.governance.verify import verify_merge_approval
from aico.intake import new_ticket_id
from aico.tickets import TicketExists, TicketNotFound, TicketStore, TransitionError

S = TicketStatus
ALLOWED_DECISIONS = {
    S.AWAITING_PLAN_APPROVAL: {Decision.APPROVED, Decision.REVISE, Decision.REJECTED},
    S.AWAITING_MERGE_APPROVAL: {
        Decision.APPROVED,
        Decision.CHANGES_REQUESTED,
        Decision.REJECTED,
    },
}


@dataclass(frozen=True)
class ApiDeps:
    tickets: TicketStore
    approvals: ApprovalStore
    api_token: str | None = None
    internal_token: str | None = None
    default_project: str | None = None
    # Trạng thái worker cho GUI; None khi không dùng worker CLI.
    workers: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None
    cors_origins: tuple[str, ...] = ()  # origin của GUI web được gọi API từ trình duyệt
    cors_origin_regex: str | None = None  # ví dụ mọi cổng localhost (API vẫn đòi token)


class CreateTicket(BaseModel):
    title: str = Field(min_length=1)
    body: str = ""
    project: str | None = None
    labels: list[str] = Field(default_factory=list)
    budget_usd: float = Field(default=5.0, gt=0)


class DecisionIn(BaseModel):
    decision: Decision
    actor: str = Field(min_length=1)
    comment: str = ""


class EscalationAction(BaseModel):
    action: str = Field(pattern="^(close|fail)$")
    actor: str = Field(min_length=1)
    comment: str = ""


def _bearer_ok(authorization: str | None, expected: str | None) -> bool:
    if expected is None:
        return True
    if not authorization or not authorization.lower().startswith("bearer "):
        return False
    return secrets.compare_digest(authorization[7:].strip(), expected)


def create_api_app(deps: ApiDeps) -> FastAPI:
    app = FastAPI(title="AI Company OS")
    if deps.cors_origins or deps.cors_origin_regex:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(deps.cors_origins),
            allow_origin_regex=deps.cors_origin_regex,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )

    def require_user(authorization: Annotated[str | None, Header()] = None) -> None:
        if not _bearer_ok(authorization, deps.api_token):
            raise HTTPException(401, "cần API token")

    def require_internal(authorization: Annotated[str | None, Header()] = None) -> None:
        if deps.internal_token is None or not _bearer_ok(authorization, deps.internal_token):
            raise HTTPException(401, "cần token nội bộ")

    async def get_ticket(ticket_id: str) -> Ticket:
        try:
            return await deps.tickets.get(ticket_id)
        except TicketNotFound:
            raise HTTPException(404, f"không có ticket {ticket_id}") from None

    user = [Depends(require_user)]

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/workers", dependencies=user)
    async def list_workers() -> list[dict[str, Any]]:
        return await deps.workers() if deps.workers is not None else []

    @app.post("/tickets", status_code=201, dependencies=user)
    async def create_ticket(body: CreateTicket) -> Ticket:
        ticket = Ticket(
            id=new_ticket_id(),
            title=body.title,
            body=body.body,
            project=body.project or deps.default_project,
            labels=body.labels,
            budget_usd=body.budget_usd,
        )
        try:
            return await deps.tickets.create(ticket, actor="api")
        except TicketExists:
            raise HTTPException(409, "trùng id, thử lại") from None

    @app.get("/tickets", dependencies=user)
    async def list_tickets(status: TicketStatus | None = None) -> list[Ticket]:
        return await deps.tickets.list(statuses={status} if status else None)

    @app.get("/tickets/{ticket_id}", dependencies=user)
    async def read_ticket(ticket_id: str) -> Ticket:
        return await get_ticket(ticket_id)

    @app.get("/tickets/{ticket_id}/events", dependencies=user)
    async def read_events(ticket_id: str) -> list[Event]:
        await get_ticket(ticket_id)
        return await deps.tickets.events(ticket_id)

    @app.post("/tickets/{ticket_id}/decisions", status_code=202, dependencies=user)
    async def decide(ticket_id: str, body: DecisionIn) -> Ticket:
        ticket = await get_ticket(ticket_id)
        allowed = ALLOWED_DECISIONS.get(ticket.status)
        if allowed is None:
            raise HTTPException(409, f"ticket đang '{ticket.status}', không chờ duyệt")
        if body.decision not in allowed:
            raise HTTPException(422, f"quyết định '{body.decision}' không hợp lệ ở bước này")
        if ticket.pending_resume is not None:
            raise HTTPException(409, "đã có quyết định đang chờ xử lý")
        payload: dict[str, Any] = body.model_dump(mode="json")
        return await deps.tickets.set_pending_resume(ticket_id, payload, actor=body.actor)

    @app.post("/tickets/{ticket_id}/escalation", dependencies=user)
    async def resolve_escalation(ticket_id: str, body: EscalationAction) -> Ticket:
        ticket = await get_ticket(ticket_id)
        if ticket.status is not S.ESCALATED:
            raise HTTPException(409, "ticket không ở trạng thái escalated")
        target = S.DONE if body.action == "close" else S.FAILED
        try:
            return await deps.tickets.transition(
                ticket_id, target, actor=body.actor, data={"comment": body.comment}
            )
        except TransitionError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/internal/approvals/{approval_id}", dependencies=[Depends(require_internal)])
    async def verify_approval(
        approval_id: str, project: Annotated[str, Query()], mr_iid: Annotated[int, Query()]
    ) -> dict[str, Any]:
        valid, reason = await verify_merge_approval(
            deps.approvals, deps.tickets, approval_id, project, mr_iid
        )
        return {"valid": valid, "reason": reason}

    return app
