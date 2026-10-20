"""Cấu hình công ty dưới dạng model: vai trò, phòng ban, SOP, policy."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aico.core.models import ActionLevel

_ID_PATTERN = r"^[a-z][a-z0-9_]*$"


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolConstraints(_Spec):
    repos: tuple[str, ...] = ()
    branch_pattern: str | None = None


class ToolGrant(_Spec):
    server: str
    allow: tuple[str, ...]
    constraints: ToolConstraints = ToolConstraints()

    @field_validator("allow")
    @classmethod
    def _non_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("allow không được rỗng")
        return value


class RoleBudget(_Spec):
    per_ticket_usd: float = Field(gt=0)
    monthly_usd: float = Field(gt=0)
    per_ticket_tokens: int | None = Field(default=None, gt=0)


class RoleLimits(_Spec):
    max_iterations: int = Field(default=40, gt=0)
    timeout_minutes: int = Field(default=45, gt=0)


class RoleSpec(_Spec):
    id: str = Field(pattern=_ID_PATTERN)
    department: str = Field(pattern=_ID_PATTERN)
    reports_to: str | None = None
    worker: Literal["llm", "cli", "openhands", "system"]
    # worker: cli — các worker subscription (claude, codex, cursor) theo thứ tự ưu tiên.
    workers: tuple[str, ...] = ()
    model: str | None = None
    allowed_models: tuple[str, ...] = ()
    prompt: str | None = None
    tools: tuple[ToolGrant, ...] = ()
    max_risk: ActionLevel = ActionLevel.L0
    budget: RoleBudget | None = None
    limits: RoleLimits = RoleLimits()

    def models(self) -> tuple[str, ...]:
        """Các model vai trò được gọi qua model relay."""
        if self.model is None:
            return self.allowed_models
        return (self.model, *[m for m in self.allowed_models if m != self.model])

    def grant_for(self, server: str) -> ToolGrant | None:
        return next((g for g in self.tools if g.server == server), None)


class Department(_Spec):
    id: str = Field(pattern=_ID_PATTERN)
    name: str
    head: str
    enabled: bool = True


class OrgSpec(_Spec):
    company: str
    board: tuple[str, ...] = Field(min_length=1)
    departments: tuple[Department, ...] = Field(min_length=1)


class SopLimits(_Spec):
    max_plan_rounds: int = Field(default=2, ge=1)
    max_review_rounds: int = Field(default=3, ge=1)
    max_merge_attempts: int = Field(default=2, ge=1)
    approval_deadline_hours: float = Field(default=48, gt=0)


class SopSpec(_Spec):
    id: str = Field(pattern=_ID_PATTERN)
    department: str
    graph: Literal["fix_ticket"]
    steps: dict[str, str]
    limits: SopLimits = SopLimits()


class ModelPrice(_Spec):
    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)


class RiskRule(_Spec):
    """Luật phân loại rủi ro ticket theo nhãn hoặc từ khóa (GĐ0–GĐ1)."""

    risk: Literal["low", "medium", "high"]
    labels: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()


class PolicySpec(_Spec):
    tool_levels: dict[str, ActionLevel]
    model_prices: dict[str, ModelPrice] = Field(default_factory=dict)
    default_risk: Literal["low", "medium", "high"] = "medium"
    risk_rules: tuple[RiskRule, ...] = ()

    def level_of(self, server: str, tool: str) -> ActionLevel | None:
        return self.tool_levels.get(f"{server}.{tool}")
