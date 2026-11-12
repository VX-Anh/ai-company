"""SOP fix_ticket thành StateGraph.

Quy ước:
- Node chuyển ticket sang trạng thái của chính nó lúc bắt đầu (chạy lại là no-op).
- Node chờ duyệt không tự chuyển trạng thái chờ: runner làm việc đó sau khi checkpoint có
  interrupt đã lưu, để tiến trình chết giữa chừng không để ticket kẹt ở trạng thái chờ.
- Mọi side effect ra ngoài mang idempotency key lấy từ state đã checkpoint.
"""

from __future__ import annotations

import asyncio
import binascii
import tarfile
from dataclasses import dataclass, field, replace
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from aico.core import ApprovalKind, Artifact, Decision, SopSpec, Ticket, TicketRisk, TicketStatus
from aico.governance import ApprovalStore
from aico.orchestrator import prereview
from aico.orchestrator.ports import (
    NO_CHANGE_ERROR,
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
    TriageResult,
)
from aico.orchestrator.state import TicketState
from aico.orchestrator.workplan import (
    ALL_FILES,
    ARCHITECTURE_DOC,
    ARCHITECTURE_LABEL,
    EVAL_SYSTEM_ROLE,
    FIX_ITEM_ID,
    WorkItem,
    clamp_owns,
    eval_spec,
    fix_item,
    parse_work_plan,
    plan_doc_path,
    render_architecture_doc,
    render_plan_doc,
    ticket_refs,
)
from aico.tickets import TicketStore
from aico.tools import ToolDenied, ToolError, ToolGateway

S = TicketStatus
GITLAB = "gitlab"
UNFINISHED = prereview.UNFINISHED_ITEMS
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
    # Số vòng review tối đa riêng theo nhãn (vd `research`: Fact Checker kỹ, cần nhiều vòng hơn);
    # không khớp nhãn nào thì dùng max_review_rounds của SOP.
    label_review_rounds: dict[str, int] = field(default_factory=dict)
    # Ticket mang nhãn này chỉ được sửa file trong các thư mục này, ở mọi vòng (vd `research`:
    # chỉ `cards/`). Vòng sửa mặc định cho sửa mọi file nên phải chặn ở đây.
    label_scopes: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def role(self, step: str) -> str:
        return self.sop.steps[step]

    def item_roles_for(self, ticket: Ticket) -> tuple[str, ...]:
        by_label = {k.lower(): v for k, v in self.label_item_roles.items()}
        for label in ticket.labels:
            if (roles := by_label.get(label.lower())) is not None:
                return roles
        return self.item_roles

    def review_rounds_for(self, ticket: Ticket) -> int:
        by_label = {k.lower(): v for k, v in self.label_review_rounds.items()}
        for label in ticket.labels:
            if (rounds := by_label.get(label.lower())) is not None:
                return rounds
        return self.sop.limits.max_review_rounds

    def scope_for(self, ticket: Ticket) -> tuple[str, ...] | None:
        by_label = {k.lower(): v for k, v in self.label_scopes.items()}
        for label in ticket.labels:
            if (scope := by_label.get(label.lower())) is not None:
                return scope
        return None

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
        is_eval = eval_spec(ticket) is not None
        if is_eval:
            # Ticket chạy lại đo hệ chứ không đo người duyệt kế hoạch, và không bao giờ merge.
            # Quyết định ở code (nhãn + artifact), không ở luật YAML theo nhãn mà ai cũng gắn được.
            result = TriageResult(result.department, TicketRisk.LOW, "chạy lại (eval)")
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
            "eval": is_eval,
            "gate_rounds": 0,
            "gate_feedback": "",
            "gate_retry_items": [],
            "gate_retried": [],
            "gate_accepted": [],
            "gate_feedback_log": "",
            "item_results": [],
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
        base = ticket_refs(ticket, deps.target_branch).base
        return await file_action(tid, project, base, ARCHITECTURE_DOC) == "create"

    async def git(tid: str, tool: str, args: dict[str, Any], *, role: str | None = None) -> Any:
        """Gọi tool Git của bước code (hoặc của `role`), thử lại khi lỗi (GitHub trễ ref, mạng
        chập chờn).

        create_branch, push (theo idempotency_key), open_merge_request đều idempotent nên thử
        lại không tạo bản trùng.
        """
        for delay in (*deps.git_retry_delays, None):
            try:
                return await deps.gateway.call(
                    role or deps.role("code"), GITLAB, tool, args, ticket_id=tid
                )
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
        refs = ticket_refs(ticket, deps.target_branch)
        try:
            if refs.eval_variant is not None:
                # Chạy lại: nhánh đích của MR dựng ở commit gốc để diff chỉ gồm phần agent làm.
                # Tạo bằng vai trò hệ thống, trước ai/<id>: vai trò của agent không có base/*.
                await git(
                    tid,
                    "create_branch",
                    {"project": project, "branch": refs.target, "ref": refs.base},
                    role=EVAL_SYSTEM_ROLE,
                )
            await git(
                tid,
                "create_branch",
                {"project": project, "branch": branch, "ref": refs.base},
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
                    "target_branch": refs.target,
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
        if mr.get("target_branch") not in (None, refs.target):
            # MR có sẵn (agent tự mở) vào đích khác: không nhận, nhất là ticket chạy lại vào main.
            return {
                "code_attempts": attempt,
                "escalation_reason": (
                    f"MR !{mr.get('iid')} của {branch} trỏ vào '{mr.get('target_branch')}', "
                    f"phải là '{refs.target}'"
                ),
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
        retry = set(s.get("gate_retry_items") or [])
        if feedback and retry:
            # Cổng trước review trả về vì đầu việc chưa xong: chạy lại đúng các đầu việc đó
            # với owns cũ (phụ thuộc vào đầu việc đã xong thì bỏ, phần đó đã nằm trên nhánh).
            work = [
                replace(i, depends_on=tuple(d for d in i.depends_on if d in retry))
                for i in items
                if i.id in retry
            ]
        else:
            work = [fix_item(feedback, default_role)] if feedback else items
        if (scope := deps.scope_for(ticket)) is not None:
            work = [clamp_owns(item, scope) for item in work]
        try:
            results = await coder.run_items(ticket, s.get("plan", ""), work, feedback)
        except BudgetExhausted as exc:
            return {"escalation_reason": f"viết code hết ngân sách: {exc}"}
        except AgentUnavailable as exc:
            return {"escalation_reason": f"viết code: {exc}"}
        rows = [r.doc_row() for r in results]
        if feedback:
            # Bảng đầu việc = lần trước, đè kết quả các đầu việc vừa chạy (vòng sửa thêm dòng FIX):
            # đầu việc còn dở không mất dấu sau một vòng sửa.
            fresh = {r["id"]: r for r in rows}
            kept = [r for r in s.get("item_results") or [] if r["id"] != FIX_ITEM_ID]
            rows = [fresh.pop(r["id"], r) for r in kept] + list(fresh.values())
        produced = [r for r in results if r.changes]
        if not produced:
            if feedback and retry and all(r.error == NO_CHANGE_ERROR for r in results):
                # Chạy lại mà agent vẫn không sửa gì (thấy không cần sửa, hoặc owns sai): nhánh
                # giữ nguyên, cổng sẽ cho tới review kèm ghi chú thay vì escalate.
                return {"code_attempts": attempt, "item_results": rows}
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
            rows,
            attempt=attempt,
            notes=list(s.get("plan_notes") or []),
        )
        docs = [(plan_doc_path(tid), doc)]
        if s.get("architecture") and await architecture_writable(tid, ticket, project):
            docs.append((ARCHITECTURE_DOC, render_architecture_doc(s["architecture"], tid)))
        update = await publish(
            tid, ticket, project, branch, attempt, commits, description=doc, docs=docs
        )
        return {**update, "item_results": rows}

    async def code(s: TicketState) -> dict[str, Any]:
        tid = s["ticket_id"]
        role = deps.role("code")
        attempt = s.get("code_attempts", 0) + 1
        # Góp ý của cổng trước review đi trước: nhánh chưa tới tay reviewer.
        feedback = s.get("gate_feedback") or s.get("review_notes") or s.get("merge_error") or None
        # Góp ý (review cần sửa, người yêu cầu sửa, merge lỗi) hiện trên /events cho người xem.
        data: dict[str, Any] = {"attempt": attempt}
        if feedback:
            data["feedback"] = feedback
        await deps.tickets.transition(tid, S.IN_PROGRESS, actor=role, data=data)
        project = await project_of(tid)
        ticket = await deps.tickets.get(tid)
        branch = ticket_refs(ticket, deps.target_branch).work
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
        update = await publish(
            tid, ticket, project, branch, attempt, commits, description=s.get("plan", "")
        )
        return {**update, "item_results": []}

    async def gate(s: TicketState) -> dict[str, Any]:
        """Cổng trước review: kiểm tất định nhánh làm việc, không gọi model (xem prereview)."""
        tid = s["ticket_id"]
        cfg = deps.sop.prereview_gate
        ticket = await deps.tickets.get(tid)
        go: dict[str, Any] = {
            "gate_passed": True,
            "gate_rounds": 0,
            "gate_feedback": "",
            "gate_retry_items": [],
        }
        skip = {label.lower() for label in cfg.skip_labels}
        if not cfg.enabled or skip & {label.lower() for label in ticket.labels}:
            return go
        modes = {
            prereview.DELETIONS: cfg.deletions,
            prereview.OUTSIDE_OWNS: cfg.outside_owns,
            prereview.UNFINISHED_ITEMS: cfg.unfinished_items,
        }
        diff_checks = cfg.deletions != "off" or cfg.outside_owns != "off"
        project = ticket.project or deps.default_project
        refs = ticket_refs(ticket, deps.target_branch)
        items = [WorkItem.from_dict(d) for d in s.get("work_items") or []]
        if (scope := deps.scope_for(ticket)) is not None:
            # Như bước code: nhãn có phạm vi riêng (vd research: cards/) thì owns bị kẹp vào đó.
            items = [clamp_owns(i, scope) for i in items] or [
                clamp_owns(WorkItem("W1", "", owns=(ALL_FILES,)), scope)
            ]
        notes: list[str] = []
        diff: prereview.BranchDiff | None = None
        stale = False
        diff_skipped = False
        if diff_checks or cfg.stale_base != "off":
            try:
                # Diff so với commit chung, không so với đầu nhánh đích: việc người khác merge
                # sau khi nhánh tách ra không bị tính là nhánh này xoá.
                fork = (
                    await git(
                        tid,
                        "merge_base",
                        {"project": project, "base": refs.target, "head": refs.work},
                    )
                )["sha"]
                if cfg.stale_base != "off":
                    head = await git(tid, "branch_head", {"project": project, "ref": refs.target})
                    stale = head["sha"] != fork
                if diff_checks:
                    base_archive = await git(
                        tid, "download_archive", {"project": project, "ref": fork}
                    )
                    head_archive = await git(
                        tid, "download_archive", {"project": project, "ref": refs.work}
                    )
                    diff = await asyncio.to_thread(
                        prereview.branch_diff, base_archive["data"], head_archive["data"]
                    )
            except (ToolError, ToolDenied, tarfile.TarError, binascii.Error, KeyError) as exc:
                # Lỗi hạ tầng không phải lỗi của bài làm: không chặn, ghi lại để người biết.
                diff_skipped = True
                notes.append(f"không lấy được diff của nhánh, bỏ kiểm tra theo diff: {exc}")
        if stale:
            notes.append(
                f"nhánh {refs.work} dựng trên gốc cũ: '{refs.target}' đã có commit mới hơn "
                "điểm tách nhánh (cần cập nhật nhánh trước khi merge)"
            )
        # Diff cộng dồn cả nhánh nên góp ý cũng phải cộng dồn: file reviewer yêu cầu sửa ở vòng
        # trước vẫn là có chủ đích ở các vòng sau.
        log = s.get("gate_feedback_log", "")
        for text in (s.get("review_notes", ""), s.get("merge_error", "")):
            if text and text not in log:
                log = f"{log}\n{text}".strip()
        accepted = s.get("gate_accepted")
        if accepted is None and diff is not None:
            # Ticket có từ trước khi có cổng: không xét lại thay đổi cũ đã qua tay reviewer.
            accepted = list(diff.paths)
            notes.append("ticket có từ trước khi có cổng: nhận mọi thay đổi đang có trên nhánh")
        result = prereview.check_branch(
            diff,
            items,
            list(s.get("item_results") or []),
            system_paths=(plan_doc_path(tid), ARCHITECTURE_DOC),
            intent_text=f"{ticket.title}\n{ticket.body}\n{s.get('plan', '')}",
            feedback_text=log,
            accepted=frozenset(accepted or ()),
            retried=frozenset(s.get("gate_retried") or ()),
            deletions=cfg.deletions != "off",
            outside_owns=cfg.outside_owns != "off",
            unfinished_items=cfg.unfinished_items != "off",
        )
        notes += result.notes
        blocking = prereview.GateResult(
            tuple(v for v in result.violations if modes[v.check] == "block")
        )
        rounds = s.get("gate_rounds", 0)
        data: dict[str, Any] = {
            "passed": blocking.passed,
            "attempt": s.get("code_attempts", 0),
            "round": rounds,
            "stale_base": stale,
            "diff_skipped": diff_skipped,
            "violations": [{**v.to_dict(), "mode": modes[v.check]} for v in result.violations],
            # Kiểm tra ở mức warn có vi phạm: lẽ ra đã trả về nếu bật block.
            "would_block": [v.check for v in result.violations if modes[v.check] == "warn"],
            "notes": notes,
        }
        if blocking.passed:
            await deps.tickets.record(tid, "gate", actor="orchestrator", data=data)
            go["gate_feedback_log"] = log
            if diff is not None:
                # Nhánh tới tay reviewer: từ đây cổng chỉ xét phần thay đổi mới.
                go["gate_accepted"] = sorted({*(accepted or ()), *diff.paths})
            elif accepted is not None:
                go["gate_accepted"] = accepted
            return go
        feedback = prereview.render_feedback(blocking, original_ref=refs.target)
        data["feedback"] = feedback
        update: dict[str, Any] = {
            "gate_passed": False,
            "gate_feedback": feedback,
            "gate_feedback_log": log,
        }
        if s.get("gate_accepted") is None and accepted is not None:
            update["gate_accepted"] = accepted  # ticket cũ: chốt phần đã nhận ở lần kiểm đầu
        if rounds >= cfg.max_rounds:
            data["escalated"] = True
            await deps.tickets.record(tid, "gate", actor="orchestrator", data=data)
            return {
                **update,
                "escalation_reason": (
                    f"{prereview.GATE_LABEL}: còn vi phạm sau {rounds} lần trả về bước code "
                    f"({prereview.summary(blocking)})"
                ),
            }
        await deps.tickets.record(tid, "gate", actor="orchestrator", data=data)
        # Vi phạm về file sửa trước bằng một vòng FIX; đầu việc dở vẫn nằm trong bảng đầu việc và
        # được chạy lại ở lần trả về sau, khi không còn vi phạm về file.
        retry = list(blocking.violations[0].paths) if blocking.only(UNFINISHED) else []
        return {
            **update,
            "gate_rounds": rounds + 1,
            "gate_retry_items": retry,
            "gate_retried": sorted({*(s.get("gate_retried") or ()), *retry}),
        }

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
            "max_review_rounds": deps.review_rounds_for(ticket),
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
        if s.get("gate_feedback"):
            data["gate_feedback"] = s["gate_feedback"]
        await deps.tickets.transition(s["ticket_id"], S.ESCALATED, actor="orchestrator", data=data)
        return {"escalation_reason": reason}

    async def finish(s: TicketState) -> dict[str, Any]:
        await deps.tickets.transition(s["ticket_id"], S.DONE, actor="orchestrator")
        return {}

    def max_review_rounds(s: TicketState) -> int:
        return s.get("max_review_rounds") or limits.max_review_rounds

    def route_analyze(s: TicketState) -> str:
        return "escalate" if s.get("escalation_reason") else "plan"

    def route_plan(s: TicketState) -> str:
        if s.get("escalation_reason"):
            return "escalate"
        return "code" if s["risk"] == "low" else "approve_plan"

    def route_code(s: TicketState) -> str:
        return "escalate" if s.get("escalation_reason") else "gate"

    def route_gate(s: TicketState) -> str:
        if s.get("escalation_reason"):
            return "escalate"
        return "review" if s.get("gate_passed", True) else "code"

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
        return "code" if s["review_rounds"] < max_review_rounds(s) else "escalate"

    def route_test(s: TicketState) -> str:
        if s.get("escalation_reason"):
            return "escalate"
        if s["test_passed"]:
            return "approve_merge"
        return "code" if s["test_rounds"] < limits.max_test_rounds else "escalate"

    def route_merge_approval(s: TicketState) -> str:
        if s.get("eval"):
            return "escalate"  # ticket chạy lại dừng ở đây dù quyết định là gì: không bao giờ merge
        if s["merge_decision"] == Decision.APPROVED:
            return "merge"
        if s["merge_decision"] == Decision.CHANGES_REQUESTED and s[
            "review_rounds"
        ] < max_review_rounds(s):
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
        ("gate", gate),
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
    g.add_conditional_edges("code", route_code, ["gate", "escalate"])
    g.add_conditional_edges("gate", route_gate, ["review", "code", "escalate"])
    g.add_conditional_edges("review", route_review, ["test", "approve_merge", "code", "escalate"])
    g.add_conditional_edges("test", route_test, ["approve_merge", "code", "escalate"])
    g.add_conditional_edges("approve_merge", route_merge_approval, ["merge", "code", "escalate"])
    g.add_conditional_edges("merge", route_merge, ["finish", "code", "escalate"])
    g.add_edge("escalate", END)
    g.add_edge("finish", END)
    return g


def _escalation_reason(s: TicketState, sop: SopSpec) -> str:
    if s.get("eval") and s.get("merge_decision"):
        return f"eval: kết thúc ở bước duyệt merge ({s['merge_decision']}), không merge"
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
