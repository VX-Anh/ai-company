"""SOP fix_ticket thành StateGraph.

Quy ước:
- Node chuyển ticket sang trạng thái của chính nó lúc bắt đầu (chạy lại là no-op).
- Node chờ duyệt không tự chuyển trạng thái chờ: runner làm việc đó sau khi checkpoint có
  interrupt đã lưu, để tiến trình chết giữa chừng không để ticket kẹt ở trạng thái chờ.
- Mọi side effect ra ngoài mang idempotency key lấy từ state đã checkpoint.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from aico.core import ApprovalKind, Artifact, Decision, SopSpec, Ticket, TicketStatus
from aico.governance import ApprovalStore
from aico.orchestrator.ports import (
    AgentUnavailable,
    Analyst,
    BudgetExhausted,
    Coder,
    FileChange,
    ItemCoder,
    Planner,
    Previewer,
    Reviewer,
    Triage,
)
from aico.orchestrator.state import TicketState
from aico.orchestrator.workplan import (
    ARCHITECTURE_DOC,
    ARCHITECTURE_LABEL,
    WorkItem,
    fix_item,
    parse_work_plan,
    plan_doc_path,
    render_architecture_doc,
    render_plan_doc,
)
from aico.tickets import TicketStore
from aico.tools import ToolError, ToolGateway

S = TicketStatus
GITLAB = "gitlab"
APPROVAL_NOTE_HEADING = "## Ghi chú của người duyệt (ưu tiên hơn kế hoạch khi mâu thuẫn)"


@dataclass(frozen=True)
class Deps:
    sop: SopSpec
    tickets: TicketStore
    approvals: ApprovalStore
    gateway: ToolGateway
    triage: Triage
    planner: Planner
    coder: Coder
    reviewer: Reviewer
    default_project: str = "group/pilot-repo"
    target_branch: str = "main"
    # Vai trò được giao đầu việc trong kế hoạch có cấu trúc; phần tử đầu là mặc định.
    item_roles: tuple[str, ...] = ("coder",)
    # Bản xem trước của MR (máy build từ xa); None thì không dựng.
    previewer: Previewer | None = None
    # Chờ giữa các lần thử lại tool Git ở bước code (giây); hết lượt thì escalate.
    git_retry_delays: tuple[float, ...] = (2.0, 5.0)
    # BA phân tích yêu cầu trước khi lập kế hoạch (bước `analyze` của SOP); None thì bỏ qua.
    analyst: Analyst | None = None
    # Ticket mang nhãn này không qua BA (vd `retro`: Researcher tự phân tích lịch sử).
    analyze_skip_labels: tuple[str, ...] = ()
    # Tester kiểm thử MR sau khi review đạt (bước `test` của SOP); None thì bỏ qua.
    tester: Reviewer | None = None
    # Ticket mang nhãn này giao đầu việc cho vai trò khác (vd `docs`: researcher, ba, tech_lead
    # viết tài liệu); phần tử đầu là mặc định và làm vòng sửa sau review.
    label_item_roles: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Ticket mang nhãn này bỏ bước test (vd `docs`: không có hành vi để chạy thử).
    test_skip_labels: tuple[str, ...] = ()

    def role(self, step: str) -> str:
        return self.sop.steps[step]

    def item_roles_for(self, ticket: Ticket) -> tuple[str, ...]:
        by_label = {k.lower(): v for k, v in self.label_item_roles.items()}
        for label in ticket.labels:
            if (roles := by_label.get(label.lower())) is not None:
                return roles
        return self.item_roles

    def skips_test(self, ticket: Ticket) -> bool:
        skip = {label.lower() for label in self.test_skip_labels}
        return bool(skip & {label.lower() for label in ticket.labels})


def _decision(raw: Any) -> tuple[Decision, str, str]:
    if not isinstance(raw, dict):
        return Decision.REJECTED, "system", "giá trị resume không hợp lệ"
    try:
        decision = Decision(raw.get("decision"))
    except ValueError:
        return Decision.REJECTED, "system", f"quyết định không hợp lệ: {raw.get('decision')}"
    return decision, str(raw.get("actor", "unknown")), str(raw.get("comment", ""))


def build_fix_ticket(deps: Deps) -> StateGraph[TicketState]:
    limits = deps.sop.limits

    async def project_of(ticket_id: str) -> str:
        ticket = await deps.tickets.get(ticket_id)
        return ticket.project or deps.default_project

    async def triage(s: TicketState) -> dict[str, Any]:
        tid = s["ticket_id"]
        role = deps.role("triage")
        ticket = await deps.tickets.get(tid)
        result = await deps.triage.triage(ticket)
        await deps.tickets.update(
            tid,
            actor=role,
            risk=result.risk,
            department=result.department,
            owner_role=deps.role("plan"),
        )
        await deps.tickets.transition(
            tid, S.TRIAGED, actor=role, data={"risk": result.risk.value, "reason": result.reason}
        )
        return {
            "risk": result.risk.value,
            "plan_rounds": 0,
            "code_attempts": 0,
            "review_rounds": 0,
            "test_rounds": 0,
            "merge_approval_rounds": 0,
            "merge_attempts": 0,
            "merged": False,
        }

    async def analyze(s: TicketState) -> dict[str, Any]:
        # Ticket rủi ro thấp (sửa nhỏ, không chờ duyệt kế hoạch) không cần BA.
        ticket = await deps.tickets.get(s["ticket_id"])
        skip = {label.lower() for label in deps.analyze_skip_labels}
        if (
            deps.analyst is None
            or "analyze" not in deps.sop.steps
            or s["risk"] == "low"
            or skip & {label.lower() for label in ticket.labels}
        ):
            return {"requirements": ""}
        try:
            text = await deps.analyst.analyze(ticket)
        except BudgetExhausted as exc:
            return {"escalation_reason": f"phân tích yêu cầu hết ngân sách: {exc}"}
        except AgentUnavailable as exc:
            return {"escalation_reason": f"phân tích yêu cầu: không worker nào làm được: {exc}"}
        return {"requirements": text}

    async def plan(s: TicketState) -> dict[str, Any]:
        tid = s["ticket_id"]
        requirements = s.get("requirements", "")
        # Yêu cầu của BA hiện trên /events một lần, ở lần lập kế hoạch đầu.
        data = {"requirements": requirements} if requirements and not s["plan_rounds"] else None
        await deps.tickets.transition(tid, S.PLANNED, actor=deps.role("plan"), data=data)
        ticket = await deps.tickets.get(tid)
        try:
            text = await deps.planner.plan(ticket, s.get("plan_feedback") or None, requirements)
        except BudgetExhausted as exc:
            return {"escalation_reason": f"lập kế hoạch hết ngân sách: {exc}"}
        except AgentUnavailable as exc:
            return {"escalation_reason": f"lập kế hoạch: không worker nào làm được: {exc}"}
        work = parse_work_plan(text, deps.item_roles_for(ticket))
        return {
            "plan": text,
            "plan_rounds": s.get("plan_rounds", 0) + 1,
            "work_items": [item.to_dict() for item in work.items],
            "architecture": work.architecture or "",
            "plan_notes": list(work.notes),
        }

    async def approve_plan(s: TicketState) -> dict[str, Any]:
        # Chạy lại từ đầu khi resume: không đặt side effect trước interrupt().
        raw = interrupt(
            {
                "kind": ApprovalKind.PLAN.value,
                "status": S.AWAITING_PLAN_APPROVAL.value,
                "ticket_id": s["ticket_id"],
                "round": s["plan_rounds"],
                "plan": s["plan"],
                "requirements": s.get("requirements", ""),
                "work_items": s.get("work_items", []),
                "deadline_hours": limits.approval_deadline_hours,
            }
        )
        decision, actor, comment = _decision(raw)
        await deps.approvals.record(
            s["ticket_id"],
            ApprovalKind.PLAN,
            s["plan_rounds"],
            decision,
            actor=actor,
            comment=comment,
        )
        update: dict[str, Any] = {"plan_decision": decision.value, "plan_feedback": comment}
        # Ghi chú kèm lệnh duyệt là chỉnh sửa nhỏ của người duyệt: gắn vào kế hoạch để Coder,
        # Reviewer, Tester và tài liệu kế hoạch đều thấy, thay vì chỉ nằm trong bản ghi duyệt.
        if decision is Decision.APPROVED and comment.strip():
            update["plan"] = f"{s['plan']}\n\n{APPROVAL_NOTE_HEADING}\n\n{comment.strip()}"
        return update

    async def file_action(tid: str, project: str, branch: str, path: str) -> str:
        """create | update cho file docs (GitLab phân biệt hai loại; GitHub thì không)."""
        try:
            await deps.gateway.call(
                deps.role("code"),
                GITLAB,
                "read_file",
                {"project": project, "path": path, "ref": branch},
                ticket_id=tid,
            )
        except ToolError:
            return "create"
        return "update"

    async def architecture_writable(tid: str, ticket: Ticket, project: str) -> bool:
        """Ghi sơ đồ khi ticket có nhãn architecture hoặc nhánh đích chưa có file kiến trúc."""
        if ARCHITECTURE_LABEL in ticket.labels:
            return True
        return await file_action(tid, project, deps.target_branch, ARCHITECTURE_DOC) == "create"

    async def git(tid: str, tool: str, args: dict[str, Any]) -> Any:
        """Gọi tool Git của bước code, thử lại khi lỗi (GitHub trễ ref, mạng chập chờn).

        create_branch, push (theo idempotency_key), open_merge_request đều idempotent nên thử
        lại không tạo bản trùng.
        """
        for delay in (*deps.git_retry_delays, None):
            try:
                return await deps.gateway.call(deps.role("code"), GITLAB, tool, args, ticket_id=tid)
            except ToolError:
                if delay is None:
                    raise
                await asyncio.sleep(delay)

    async def publish(
        tid: str,
        ticket: Ticket,
        project: str,
        branch: str,
        attempt: int,
        commits: list[tuple[str, list[dict[str, Any]], str]],
        *,
        description: str,
        docs: list[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Tạo branch, push từng commit, mở MR. Lỗi Git sau khi thử lại: escalate và giữ thay đổi.

        Code agent viết xong không mất khi GitHub lỗi: thay đổi nằm trong event escalated để
        người xem, lấy lại hoặc đẩy tay.
        """
        role = deps.role("code")
        try:
            await git(
                tid,
                "create_branch",
                {"project": project, "branch": branch, "ref": deps.target_branch},
            )
            for message, files, key in commits:
                await git(
                    tid,
                    "push",
                    {
                        "project": project,
                        "branch": branch,
                        "commit_message": message,
                        "files": files,
                        "idempotency_key": key,
                    },
                )
            if docs:
                doc_files = [
                    FileChange(
                        path, content, await file_action(tid, project, branch, path)
                    ).to_arg()
                    for path, content in docs
                ]
                await git(
                    tid,
                    "push",
                    {
                        "project": project,
                        "branch": branch,
                        "commit_message": f"{tid}: cập nhật tài liệu kế hoạch (lần {attempt})",
                        "files": doc_files,
                        "idempotency_key": f"{tid}:docs:{attempt}",
                    },
                )
            mr = await git(
                tid,
                "open_merge_request",
                {
                    "project": project,
                    "source_branch": branch,
                    "target_branch": deps.target_branch,
                    "title": f"{tid}: {ticket.title}",
                    "description": description,
                },
            )
        except ToolError as exc:
            unpushed = [f for _, files, _ in commits for f in files]
            unpushed += [{"path": path, "action": "update", "content": c} for path, c in docs or []]
            return {
                "code_attempts": attempt,
                "escalation_reason": f"đẩy code lên Git lỗi (lần code {attempt}): {exc}",
                "unpushed_changes": unpushed,
            }
        await deps.tickets.add_artifact(tid, Artifact(kind="branch", ref=branch), actor=role)
        await deps.tickets.add_artifact(
            tid,
            Artifact(kind="merge_request", ref=f"{project}!{mr['iid']}", url=mr.get("web_url")),
            actor=role,
        )
        if deps.previewer is not None:
            deps.previewer.schedule(tid, project, branch)
        return {"code_attempts": attempt, "branch": branch, "mr_iid": mr["iid"], "merge_error": ""}

    async def code_items(
        s: TicketState,
        ticket: Ticket,
        project: str,
        branch: str,
        attempt: int,
        feedback: str | None,
    ) -> dict[str, Any]:
        """Chạy các đầu việc song song, mỗi đầu việc một commit, rồi commit tài liệu kế hoạch."""
        tid = s["ticket_id"]
        coder = cast(ItemCoder, deps.coder)
        items = [WorkItem.from_dict(d) for d in s.get("work_items") or []]
        default_role = deps.item_roles_for(ticket)[0]
        if not items:
            items = [WorkItem("W1", "Thực hiện kế hoạch", default_role)]
        work = [fix_item(feedback, default_role)] if feedback else items
        try:
            results = await coder.run_items(ticket, s.get("plan", ""), work, feedback)
        except BudgetExhausted as exc:
            return {"escalation_reason": f"viết code hết ngân sách: {exc}"}
        except AgentUnavailable as exc:
            return {"escalation_reason": f"viết code: {exc}"}
        produced = [r for r in results if r.changes]
        if not produced:
            errors = "; ".join(f"{r.item_id}: {r.error}" for r in results if r.error)
            return {"escalation_reason": f"viết code: không đầu việc nào sửa được file ({errors})"}

        commits = [
            (
                f"{tid}/{r.item_id}: {r.summary}",
                [c.to_arg() for c in r.changes],
                f"{tid}:code:{attempt}:{r.item_id}",
            )
            for r in produced
        ]
        doc = render_plan_doc(
            tid,
            ticket.title,
            s.get("plan", ""),
            [i.to_dict() for i in items],
            [r.doc_row() for r in results],
            attempt=attempt,
            notes=list(s.get("plan_notes") or []),
        )
        docs = [(plan_doc_path(tid), doc)]
        if s.get("architecture") and await architecture_writable(tid, ticket, project):
            docs.append((ARCHITECTURE_DOC, render_architecture_doc(s["architecture"], tid)))
        return await publish(
            tid, ticket, project, branch, attempt, commits, description=doc, docs=docs
        )

    async def code(s: TicketState) -> dict[str, Any]:
        tid = s["ticket_id"]
        role = deps.role("code")
        attempt = s.get("code_attempts", 0) + 1
        feedback = s.get("review_notes") or s.get("merge_error") or None
        # Góp ý (review cần sửa, người yêu cầu sửa, merge lỗi) hiện trên /events cho người xem.
        data: dict[str, Any] = {"attempt": attempt}
        if feedback:
            data["feedback"] = feedback
        await deps.tickets.transition(tid, S.IN_PROGRESS, actor=role, data=data)
        project = await project_of(tid)
        branch = f"ai/{tid}"
        ticket = await deps.tickets.get(tid)
        if isinstance(deps.coder, ItemCoder):
            return await code_items(s, ticket, project, branch, attempt, feedback)
        try:
            result = await deps.coder.code(ticket, s.get("plan", ""), feedback)
        except BudgetExhausted as exc:
            return {"escalation_reason": f"viết code hết ngân sách: {exc}"}
        except AgentUnavailable as exc:
            return {"escalation_reason": f"viết code: {exc}"}

        commits = [
            (
                f"{tid}: {result.summary}",
                [c.to_arg() for c in result.changes],
                f"{tid}:code:{attempt}",
            )
        ]
        return await publish(
            tid, ticket, project, branch, attempt, commits, description=s.get("plan", "")
        )

    async def review(s: TicketState) -> dict[str, Any]:
        tid = s["ticket_id"]
        role = deps.role("review")
        await deps.tickets.transition(tid, S.IN_REVIEW, actor=role)
        project = await project_of(tid)
        mr = await deps.gateway.call(
            role,
            GITLAB,
            "get_merge_request",
            {"project": project, "mr_iid": s["mr_iid"]},
            ticket_id=tid,
        )
        ticket = await deps.tickets.get(tid)
        try:
            result = await deps.reviewer.review(
                ticket, mr, s.get("plan", ""), s.get("requirements", "")
            )
        except BudgetExhausted as exc:
            return {"escalation_reason": f"review hết ngân sách: {exc}"}
        except AgentUnavailable as exc:
            return {"escalation_reason": f"review: {exc}"}
        rounds = s.get("review_rounds", 0) + (0 if result.passed else 1)
        return {
            "review_passed": result.passed,
            "review_notes": result.notes,
            "review_rounds": rounds,
            "skip_test": deps.skips_test(ticket),
        }

    async def test(s: TicketState) -> dict[str, Any]:
        tid = s["ticket_id"]
        role = deps.sop.steps["test"]
        tester = cast(Reviewer, deps.tester)
        await deps.tickets.transition(tid, S.IN_TESTING, actor=role)
        project = await project_of(tid)
        mr = await deps.gateway.call(
            role,
            GITLAB,
            "get_merge_request",
            {"project": project, "mr_iid": s["mr_iid"]},
            ticket_id=tid,
        )
        try:
            result = await tester.review(
                await deps.tickets.get(tid), mr, s.get("plan", ""), s.get("requirements", "")
            )
        except BudgetExhausted as exc:
            return {"escalation_reason": f"kiểm thử hết ngân sách: {exc}"}
        except AgentUnavailable as exc:
            return {"escalation_reason": f"kiểm thử: {exc}"}
        update: dict[str, Any] = {"test_passed": result.passed, "test_notes": result.notes}
        if not result.passed:
            # Coder đọc góp ý từ review_notes; lần review sau sẽ ghi đè.
            update["test_rounds"] = s.get("test_rounds", 0) + 1
            update["review_notes"] = result.notes
        return update

    async def approve_merge(s: TicketState) -> dict[str, Any]:
        round_ = s.get("merge_approval_rounds", 0) + 1
        raw = interrupt(
            {
                "kind": ApprovalKind.MERGE.value,
                "status": S.AWAITING_MERGE_APPROVAL.value,
                "ticket_id": s["ticket_id"],
                "round": round_,
                "mr_iid": s["mr_iid"],
                "review_notes": s.get("review_notes", ""),
                "test_notes": s.get("test_notes", ""),
                "deadline_hours": limits.approval_deadline_hours,
            }
        )
        decision, actor, comment = _decision(raw)
        approval = await deps.approvals.record(
            s["ticket_id"], ApprovalKind.MERGE, round_, decision, actor=actor, comment=comment
        )
        update: dict[str, Any] = {
            "merge_approval_rounds": round_,
            "merge_decision": decision.value,
            "merge_approval_id": approval.id,
        }
        if decision is Decision.CHANGES_REQUESTED:
            update["review_rounds"] = s.get("review_rounds", 0) + 1
            update["review_notes"] = comment
        return update

    async def merge(s: TicketState) -> dict[str, Any]:
        tid = s["ticket_id"]
        role = deps.role("merge")
        await deps.tickets.transition(tid, S.MERGING, actor=role)
        project = await project_of(tid)
        try:
            await deps.gateway.call(
                role,
                GITLAB,
                "merge_mr",
                {"project": project, "mr_iid": s["mr_iid"], "approval_id": s["merge_approval_id"]},
                ticket_id=tid,
            )
            mr = await deps.gateway.call(
                role,
                GITLAB,
                "get_merge_request",
                {"project": project, "mr_iid": s["mr_iid"]},
                ticket_id=tid,
            )
        except ToolError as exc:
            return {
                "merged": False,
                "merge_attempts": s.get("merge_attempts", 0) + 1,
                "merge_error": str(exc),
            }
        if mr.get("merged"):
            if deps.previewer is not None:
                deps.previewer.schedule_site(tid, project, deps.target_branch)
            return {"merged": True}
        return {
            "merged": False,
            "merge_attempts": s.get("merge_attempts", 0) + 1,
            "merge_error": f"MR ở trạng thái '{mr.get('state')}' sau khi merge",
        }

    async def escalate(s: TicketState) -> dict[str, Any]:
        reason = s.get("escalation_reason") or _escalation_reason(s, deps.sop)
        # Người xử lý escalate cần đủ ngữ cảnh: góp ý review cuối, số vòng, code chưa đẩy được.
        data: dict[str, Any] = {"reason": reason}
        if s.get("review_notes"):
            data["review_notes"] = s["review_notes"]
            data["review_rounds"] = s.get("review_rounds", 0)
        if s.get("test_notes"):
            data["test_notes"] = s["test_notes"]
            data["test_rounds"] = s.get("test_rounds", 0)
        if s.get("unpushed_changes"):
            data["unpushed_changes"] = s["unpushed_changes"]
        await deps.tickets.transition(s["ticket_id"], S.ESCALATED, actor="orchestrator", data=data)
        return {"escalation_reason": reason}

    async def finish(s: TicketState) -> dict[str, Any]:
        await deps.tickets.transition(s["ticket_id"], S.DONE, actor="orchestrator")
        return {}

    def route_analyze(s: TicketState) -> str:
        return "escalate" if s.get("escalation_reason") else "plan"

    def route_plan(s: TicketState) -> str:
        if s.get("escalation_reason"):
            return "escalate"
        return "code" if s["risk"] == "low" else "approve_plan"

    def route_code(s: TicketState) -> str:
        return "escalate" if s.get("escalation_reason") else "review"

    def route_plan_approval(s: TicketState) -> str:
        if s["plan_decision"] == Decision.APPROVED:
            return "code"
        if s["plan_decision"] == Decision.REVISE and s["plan_rounds"] < limits.max_plan_rounds:
            return "plan"
        return "escalate"

    def route_review(s: TicketState) -> str:
        if s.get("escalation_reason"):
            return "escalate"
        if s["review_passed"]:
            tests = deps.tester is not None and "test" in deps.sop.steps
            return "test" if tests and not s.get("skip_test") else "approve_merge"
        return "code" if s["review_rounds"] < limits.max_review_rounds else "escalate"

    def route_test(s: TicketState) -> str:
        if s.get("escalation_reason"):
            return "escalate"
        if s["test_passed"]:
            return "approve_merge"
        return "code" if s["test_rounds"] < limits.max_test_rounds else "escalate"

    def route_merge_approval(s: TicketState) -> str:
        if s["merge_decision"] == Decision.APPROVED:
            return "merge"
        if (
            s["merge_decision"] == Decision.CHANGES_REQUESTED
            and s["review_rounds"] < limits.max_review_rounds
        ):
            return "code"
        return "escalate"

    def route_merge(s: TicketState) -> str:
        if s.get("merged"):
            return "finish"
        return "code" if s["merge_attempts"] < limits.max_merge_attempts else "escalate"

    g: StateGraph[TicketState] = StateGraph(TicketState)
    for name, fn in (
        ("triage", triage),
        ("analyze", analyze),
        ("plan", plan),
        ("approve_plan", approve_plan),
        ("code", code),
        ("review", review),
        ("test", test),
        ("approve_merge", approve_merge),
        ("merge", merge),
        ("escalate", escalate),
        ("finish", finish),
    ):
        g.add_node(name, fn)  # type: ignore[call-overload]
    g.add_edge(START, "triage")
    g.add_edge("triage", "analyze")
    g.add_conditional_edges("analyze", route_analyze, ["plan", "escalate"])
    g.add_conditional_edges("plan", route_plan, ["code", "approve_plan", "escalate"])
    g.add_conditional_edges("approve_plan", route_plan_approval, ["code", "plan", "escalate"])
    g.add_conditional_edges("code", route_code, ["review", "escalate"])
    g.add_conditional_edges("review", route_review, ["test", "approve_merge", "code", "escalate"])
    g.add_conditional_edges("test", route_test, ["approve_merge", "code", "escalate"])
    g.add_conditional_edges("approve_merge", route_merge_approval, ["merge", "code", "escalate"])
    g.add_conditional_edges("merge", route_merge, ["finish", "code", "escalate"])
    g.add_edge("escalate", END)
    g.add_edge("finish", END)
    return g


def _escalation_reason(s: TicketState, sop: SopSpec) -> str:
    if s.get("plan_decision") in (Decision.REJECTED, Decision.TIMEOUT):
        return f"kế hoạch: {s['plan_decision']}"
    if s.get("plan_decision") == Decision.REVISE:
        return f"kế hoạch sửa quá {sop.limits.max_plan_rounds} vòng"
    if s.get("merge_decision") in (Decision.REJECTED, Decision.TIMEOUT):
        return f"merge: {s['merge_decision']}"
    if s.get("merge_attempts", 0) >= sop.limits.max_merge_attempts:
        return f"merge lỗi {s['merge_attempts']} lần: {s.get('merge_error', '')}"
    if s.get("review_passed") and s.get("test_rounds", 0) >= sop.limits.max_test_rounds:
        return f"kiểm thử chưa đạt sau {s['test_rounds']} vòng"
    return f"review chưa đạt sau {s.get('review_rounds', 0)} vòng"
