"""Ứng viên cho bộ ticket chuẩn (Phòng R&D, R1): ticket code đã merge, đủ mốc để chạy lại.

Bộ chuẩn dùng để đo một thay đổi prompt/vai trò/cách xếp phòng: chạy lại cùng đề bài từ cùng
trạng thái repo (`base_sha`), so kết quả với bản đã merge (`merge_commit_sha`) và số liệu thực tế
lúc chạy thật (`observed`). Module này chỉ liệt kê ứng viên; người chọn 10–15 ticket (đủ dễ/khó,
đủ repo) và lưu vào `eval/tickets/` của ai-company.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from aico.core import Ticket, TicketStatus
from aico.rnd.metrics import ticket_metrics
from aico.tickets import TicketStore

# Ticket không phải code (thẻ tri thức, tài liệu, hồi cứu): không đo được bằng chạy lại + test.
EXCLUDED_LABELS = frozenset({"research", "docs", "retro", "eval"})

# (project, mr_iid) -> view của MR (tool gitlab.get_merge_request: base_sha, head_sha, ...).
MrReader = Callable[[str, int], Awaitable[dict[str, Any]]]
# (project, base, head) -> SHA merge-base (tool gitlab.merge_base).
MergeBaseReader = Callable[[str, str, str], Awaitable[str]]


def merge_request_of(ticket: Ticket) -> tuple[str, int] | None:
    """MR cuối của ticket từ artifact `merge_request` (ref dạng 'owner/repo!iid')."""
    for artifact in reversed(ticket.artifacts):
        if artifact.kind != "merge_request":
            continue
        project, _, iid = artifact.ref.rpartition("!")
        if project and iid.isdigit():
            return project, int(iid)
    return None


def _tokens(jobs: list[dict[str, Any]]) -> int:
    return sum(int(j.get("tokens_in") or 0) + int(j.get("tokens_out") or 0) for j in jobs)


async def _fork_point(
    read_merge_base: MergeBaseReader | None, project: str, mr: dict[str, Any]
) -> dict[str, Any]:
    """Commit gốc để chạy lại: merge-base của MR, không phải `base_sha` mà GitHub trả.

    `base_sha` của PR là đầu nhánh đích lúc tạo/cập nhật PR; nếu nhánh làm việc tách ra từ commit
    cũ hơn thì dựng lại từ `base_sha` cho agent thấy code mà lúc chạy thật nó chưa có. So với
    `base_sha` (không phải `main`): sau khi merge, `main` đã chứa cả nhánh nên merge-base với
    `main` chính là đầu nhánh. Không đọc được merge-base thì giữ `base_sha` và ghi rõ nguồn.
    """
    pr_base = str(mr["base_sha"])
    head = mr.get("head_sha")
    if read_merge_base is None or not head:
        return {"base_sha": pr_base, "base_sha_source": "pr"}
    try:
        fork = await read_merge_base(project, pr_base, str(head))
    except Exception as exc:  # thiếu merge-base không làm mất ứng viên
        return {"base_sha": pr_base, "base_sha_source": "pr", "base_sha_note": str(exc)[:200]}
    out: dict[str, Any] = {"base_sha": fork, "base_sha_source": "merge_base"}
    if fork != pr_base:
        out["pr_base_sha"] = pr_base
    return out


async def candidates(
    store: TicketStore,
    read_mr: MrReader,
    *,
    since: datetime,
    read_merge_base: MergeBaseReader | None = None,
) -> list[dict[str, Any]]:
    """Ticket xong (DONE) từ `since`, có MR đã merge; cũ nhất trước. Ticket thiếu mốc bị bỏ."""
    out: list[dict[str, Any]] = []
    for ticket in await store.list(statuses={TicketStatus.DONE}):
        if ticket.created_at < since:
            continue
        if EXCLUDED_LABELS & {label.lower() for label in ticket.labels}:
            continue
        ref = merge_request_of(ticket)
        if ref is None:
            continue
        project, iid = ref
        try:
            mr = await read_mr(project, iid)
        except Exception as exc:  # một MR đọc lỗi không làm hỏng cả danh sách
            out.append({"id": ticket.id, "skipped": f"không đọc được MR !{iid}: {exc}"})
            continue
        if not mr.get("merged") or not mr.get("base_sha"):
            continue
        m = ticket_metrics(ticket, await store.events(ticket.id))
        out.append(
            {
                "id": ticket.id,
                "project": project,
                "title": ticket.title,
                "body": ticket.body,
                "labels": ticket.labels,
                "risk": ticket.risk.value if ticket.risk else None,
                "mr": {"iid": iid, "url": mr.get("web_url")},
                **await _fork_point(read_merge_base, project, mr),
                "reference_head_sha": mr.get("head_sha"),
                "merge_commit_sha": mr.get("merge_commit_sha"),
                "observed": {
                    "code_rounds": m["code_rounds"],
                    "review_rounds": m["review_rounds"],
                    "owner_returns": m["owner_returns"],
                    "minutes_to_merge_approval": m["minutes_to_merge_approval"],
                    "tokens": _tokens(m["jobs"]),
                },
            }
        )
    return out
