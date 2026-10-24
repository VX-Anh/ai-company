"""Model dữ liệu chung. Không import package nào khác của aico."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class TicketStatus(StrEnum):
    NEW = "new"
    TRIAGED = "triaged"
    PLANNED = "planned"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    IN_TESTING = "in_testing"
    AWAITING_MERGE_APPROVAL = "awaiting_merge_approval"
    MERGING = "merging"
    DONE = "done"
    ESCALATED = "escalated"
    FAILED = "failed"


TERMINAL_STATUSES = frozenset({TicketStatus.DONE, TicketStatus.FAILED})
WAITING_STATUSES = frozenset(
    {
        TicketStatus.AWAITING_PLAN_APPROVAL,
        TicketStatus.AWAITING_MERGE_APPROVAL,
        TicketStatus.ESCALATED,
    }
)


class TicketRisk(StrEnum):
    """Rủi ro của cả ticket; quyết định kế hoạch có cần người duyệt hay không."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ActionLevel(StrEnum):
    """Mức rủi ro của một hành động (tool call)."""

    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"

    @property
    def rank(self) -> int:
        return int(self.value[1])


class TicketSource(StrEnum):
    GITLAB = "gitlab"
    JIRA = "jira"
    API = "api"


class Artifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    # screenshot: ảnh giao diện reviewer chụp (ref = "<lần chụp>/<file>.png").
    # jira: issue Jira đồng bộ với ticket (ref = khoá issue, ví dụ LHV-14).
    kind: Literal["branch", "merge_request", "report", "preview", "screenshot", "jira"]
    ref: str
    url: str | None = None


class Ticket(BaseModel):
    id: str
    title: str
    body: str = ""
    source: TicketSource = TicketSource.API
    source_ref: str | None = None
    project: str | None = None
    labels: list[str] = Field(default_factory=list)
    department: str | None = None
    owner_role: str | None = None
    status: TicketStatus = TicketStatus.NEW
    risk: TicketRisk | None = None
    budget_usd: float = 5.0
    spent_usd: float = 0.0
    spent_tokens: int = 0
    parent_id: str | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    trace_id: str | None = None
    lease_owner: str | None = None
    lease_until: datetime | None = None
    pending_resume: dict[str, Any] | None = None
    approval_deadline: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class Event(BaseModel):
    ticket_id: str
    kind: str
    from_status: TicketStatus | None = None
    to_status: TicketStatus | None = None
    actor: str
    data: dict[str, Any] = Field(default_factory=dict)
    at: datetime = Field(default_factory=utcnow)


class ApprovalKind(StrEnum):
    PLAN = "plan"
    MERGE = "merge"


class Decision(StrEnum):
    APPROVED = "approved"
    REVISE = "revise"
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


class Approval(BaseModel):
    id: str
    ticket_id: str
    kind: ApprovalKind
    round: int
    decision: Decision
    actor: str
    comment: str = ""
    at: datetime = Field(default_factory=utcnow)

    @property
    def is_approved(self) -> bool:
        return self.decision is Decision.APPROVED
