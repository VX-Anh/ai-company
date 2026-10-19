"""Kiểm tra policy trước mỗi tool call: quyền, mức rủi ro, ràng buộc tham số, bản ghi duyệt."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Any

from aico.core import ActionLevel, ApprovalKind, PolicySpec, RoleSpec
from aico.governance import ApprovalStore

BRANCH_ARGS = ("branch", "source_branch")
# Tool L3 nào cần loại bản ghi duyệt nào.
APPROVAL_KIND_FOR_TOOL = {"gitlab.merge_mr": ApprovalKind.MERGE}


class Verdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    NEEDS_APPROVAL = "needs_approval"


@dataclass(frozen=True)
class GuardDecision:
    verdict: Verdict
    level: ActionLevel | None
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW


def _deny(reason: str, level: ActionLevel | None = None) -> GuardDecision:
    return GuardDecision(Verdict.DENY, level, reason)


def check_branch(branch: str, pattern: str) -> bool:
    if not branch or ".." in branch or branch.startswith("/") or branch.endswith("/"):
        return False
    return fnmatchcase(branch, pattern)


class Guard:
    def __init__(self, policy: PolicySpec, approvals: ApprovalStore) -> None:
        self._policy = policy
        self._approvals = approvals

    async def check(
        self,
        role: RoleSpec,
        server: str,
        tool: str,
        args: dict[str, Any],
        *,
        ticket_id: str | None,
    ) -> GuardDecision:
        grant = role.grant_for(server)
        if grant is None or tool not in grant.allow:
            return _deny(f"vai trò '{role.id}' không được cấp tool '{server}.{tool}'")

        level = self._policy.level_of(server, tool)
        if level is None:
            return _deny(f"tool '{server}.{tool}' chưa có mức rủi ro trong policy")
        if level.rank > role.max_risk.rank:
            return _deny(f"mức {level} vượt max_risk {role.max_risk} của '{role.id}'", level)

        constraints = grant.constraints
        project = args.get("project")
        if constraints.repos and project not in constraints.repos:
            return _deny(f"repo '{project}' nằm ngoài ràng buộc của '{role.id}'", level)
        if constraints.branch_pattern is not None:
            for name in BRANCH_ARGS:
                branch = args.get(name)
                if branch is not None and not check_branch(str(branch), constraints.branch_pattern):
                    return _deny(
                        f"branch '{branch}' không khớp ràng buộc '{constraints.branch_pattern}'",
                        level,
                    )

        if level is ActionLevel.L3:
            return await self._check_approval(server, tool, args, ticket_id, level)
        return GuardDecision(Verdict.ALLOW, level)

    async def _check_approval(
        self,
        server: str,
        tool: str,
        args: dict[str, Any],
        ticket_id: str | None,
        level: ActionLevel,
    ) -> GuardDecision:
        approval_id = args.get("approval_id")
        if not approval_id:
            return GuardDecision(Verdict.NEEDS_APPROVAL, level, "hành động L3 cần bản ghi duyệt")
        approval = await self._approvals.get(str(approval_id))
        expected_kind = APPROVAL_KIND_FOR_TOOL.get(f"{server}.{tool}")
        if approval is None or ticket_id is None or approval.ticket_id != ticket_id:
            return _deny("bản ghi duyệt không tồn tại hoặc không thuộc ticket này", level)
        if expected_kind is not None and approval.kind is not expected_kind:
            return _deny(f"bản ghi duyệt không phải loại '{expected_kind}'", level)
        if not approval.is_approved:
            return _deny(f"bản ghi duyệt có quyết định '{approval.decision}'", level)
        return GuardDecision(Verdict.ALLOW, level)
