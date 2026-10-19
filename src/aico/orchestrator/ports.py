"""Cổng tới các bước do agent làm. GĐ0 dùng bản giả lập; GĐ1 thay bằng agent thật."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from aico.core import PolicySpec, Ticket, TicketRisk


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

    def to_arg(self) -> dict[str, Any]:
        arg: dict[str, Any] = {"path": self.path, "action": self.action}
        if self.action != "delete":
            arg["content"] = self.content
        return arg


@dataclass(frozen=True)
class CodeResult:
    changes: tuple[FileChange, ...]
    summary: str


@dataclass(frozen=True)
class ReviewResult:
    passed: bool
    notes: str = ""


class BudgetExhausted(Exception):
    """Agent hết ngân sách giữa chừng: ticket dừng và hỏi người, không coi là lỗi."""


class Triage(Protocol):
    async def triage(self, ticket: Ticket) -> TriageResult: ...


class Planner(Protocol):
    async def plan(self, ticket: Ticket, feedback: str | None) -> str: ...


class Coder(Protocol):
    async def code(self, ticket: Ticket, plan: str, feedback: str | None) -> CodeResult: ...


class Reviewer(Protocol):
    async def review(self, ticket: Ticket, mr: dict[str, Any]) -> ReviewResult: ...


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

    async def review(self, ticket: Ticket, mr: dict[str, Any]) -> ReviewResult:
        if self.results:
            return self.results.pop(0)
        return ReviewResult(passed=True, notes="đạt")
