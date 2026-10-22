"""Cổng tới các bước do agent làm. GĐ0 dùng bản giả lập; GĐ1 thay bằng agent thật."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from aico.core import PolicySpec, Ticket, TicketRisk
from aico.orchestrator.workplan import WorkItem


@dataclass(frozen=True)
class TriageResult:
    department: str
    risk: TicketRisk
    reason: str = ""


@dataclass(frozen=True)
class FileChange:
    path: str
    content: str = ""
    action: str = "update"  # create | update | delete
    encoding: str = "text"  # text | base64 (file nhị phân)

    @classmethod
    def from_arg(cls, arg: dict[str, Any]) -> FileChange:
        encoding = "base64" if arg.get("encoding") == "base64" else "text"
        return cls(arg["path"], arg.get("content", ""), arg["action"], encoding)

    def to_arg(self) -> dict[str, Any]:
        arg: dict[str, Any] = {"path": self.path, "action": self.action}
        if self.action != "delete":
            arg["content"] = self.content
            if self.encoding == "base64":
                arg["encoding"] = "base64"
        return arg

    def data(self) -> bytes:
        if self.encoding == "base64":
            return base64.b64decode(self.content)
        return self.content.encode("utf-8")


@dataclass(frozen=True)
class CodeResult:
    changes: tuple[FileChange, ...]
    summary: str


@dataclass(frozen=True)
class ItemResult:
    """Kết quả một đầu việc trong kế hoạch có cấu trúc."""

    item_id: str
    title: str
    status: str  # xong | lỗi | bỏ qua
    changes: tuple[FileChange, ...] = ()
    summary: str = ""
    worker: str = ""
    error: str = ""
    rejected: tuple[str, ...] = ()  # file sửa ngoài phạm vi owns, đã bỏ

    def doc_row(self) -> dict[str, Any]:
        return {
            "id": self.item_id,
            "title": self.title,
            "status": self.status if not self.error else f"{self.status}: {self.error[:120]}",
            "worker": self.worker,
            "rejected": list(self.rejected),
        }


@dataclass(frozen=True)
class ReviewResult:
    passed: bool
    notes: str = ""


class BudgetExhausted(Exception):
    """Agent hết ngân sách giữa chừng: ticket dừng và hỏi người, không coi là lỗi."""


class AgentUnavailable(Exception):
    """Không worker nào làm được việc (hết hạn mức subscription, lỗi, bận quá lâu): hỏi người."""


class Triage(Protocol):
    async def triage(self, ticket: Ticket) -> TriageResult: ...


class Planner(Protocol):
    async def plan(self, ticket: Ticket, feedback: str | None) -> str: ...


class Coder(Protocol):
    async def code(self, ticket: Ticket, plan: str, feedback: str | None) -> CodeResult: ...


@runtime_checkable
class ItemCoder(Protocol):
    """Coder chạy được nhiều đầu việc song song theo cây phụ thuộc (worker CLI)."""

    async def run_items(
        self, ticket: Ticket, plan: str, items: list[WorkItem], feedback: str | None
    ) -> list[ItemResult]: ...


class Reviewer(Protocol):
    async def review(self, ticket: Ticket, mr: dict[str, Any], plan: str = "") -> ReviewResult:
        """plan: kế hoạch đã duyệt, là chuẩn để chấm (yêu cầu ngoài kế hoạch chỉ là gợi ý)."""
        ...


class Previewer(Protocol):
    """Dựng bản xem trước của branch MR (chạy nền, lỗi không ảnh hưởng luồng)."""

    def schedule(self, ticket_id: str, project: str, branch: str) -> None: ...

    def schedule_site(self, ticket_id: str, project: str, ref: str) -> None: ...


class RuleTriage:
    """Phân loại rủi ro bằng luật trong policy (GĐ0–GĐ1). Luật đầu tiên khớp được dùng."""

    def __init__(self, policy: PolicySpec, department: str) -> None:
        self._policy = policy
        self._department = department

    async def triage(self, ticket: Ticket) -> TriageResult:
        text = f"{ticket.title}\n{ticket.body}".lower()
        labels = {label.lower() for label in ticket.labels}
        for rule in self._policy.risk_rules:
            if labels & {label.lower() for label in rule.labels}:
                return TriageResult(self._department, TicketRisk(rule.risk), "khớp nhãn")
            if any(k.lower() in text for k in rule.keywords):
                return TriageResult(self._department, TicketRisk(rule.risk), "khớp từ khóa")
        return TriageResult(self._department, TicketRisk(self._policy.default_risk), "mặc định")


class LabelPlanner:
    """Chọn planner theo nhãn ticket (`retro` cho Researcher); không khớp thì dùng mặc định."""

    def __init__(self, default: Planner, by_label: dict[str, Planner]) -> None:
        self._default = default
        self._by_label = {label.lower(): p for label, p in by_label.items()}

    def pick(self, ticket: Ticket) -> Planner:
        for label in ticket.labels:
            if (planner := self._by_label.get(label.lower())) is not None:
                return planner
        return self._default

    async def plan(self, ticket: Ticket, feedback: str | None) -> str:
        return await self.pick(ticket).plan(ticket, feedback)


class StaticPlanner:
    async def plan(self, ticket: Ticket, feedback: str | None) -> str:
        lines = [f"Kế hoạch cho {ticket.id}: {ticket.title}", "1. Sửa code", "2. Chạy test"]
        if feedback:
            lines.append(f"Đã tính tới góp ý: {feedback}")
        return "\n".join(lines)


class ScriptedCoder:
    """Coder giả: ghi một file ghi chú. Dùng để chạy hết luồng trước khi có OpenHands."""

    async def code(self, ticket: Ticket, plan: str, feedback: str | None) -> CodeResult:
        content = f"# {ticket.id}\n\n{plan}\n"
        if feedback:
            content += f"\nSửa theo review: {feedback}\n"
        change = FileChange(path=f"notes/{ticket.id}.md", content=content, action="create")
        return CodeResult(changes=(change,), summary=f"Giả lập sửa {ticket.id}")


@dataclass
class ScriptedReviewer:
    """Reviewer giả: trả lần lượt các kết quả cho trước, hết thì luôn đạt."""

    results: list[ReviewResult] = field(default_factory=list)

    async def review(self, ticket: Ticket, mr: dict[str, Any], plan: str = "") -> ReviewResult:
        if self.results:
            return self.results.pop(0)
        return ReviewResult(passed=True, notes="đạt")
