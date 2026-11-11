"""Chỉ số hiệu năng hệ agent (Phòng R&D, R0): nền đo trước khi đổi prompt, vai trò, xếp phòng.

Chỉ đọc dữ liệu đã có: ticket, event trạng thái, quyết định của người duyệt, event `job` (một lần
giao việc cho worker: phòng, vai trò, prompt_sha, token, thời gian). Hàm thuần để test không cần DB.

Các chỉ số chính và lý do:
- first_pass_rate: ticket xong mà chỉ code một lần; đo chất lượng kế hoạch + coder.
- review_rounds: số lần vào review; vòng lặp review là chi phí lớn nhất hiện nay.
- owner_return_rate: người duyệt trả lại (changes_requested) dù Reviewer đã cho đạt; đo Reviewer
  bỏ sót. Xem cùng review_rounds để không "giảm vòng review" bằng cách làm Reviewer dễ dãi.
- escalations theo lớp lý do: biết lỗi nào lặp lại để thành giả thuyết thí nghiệm.
- jobs theo vai trò / phòng / prompt_sha: tỷ lệ lỗi, hết hạn mức, token, thời gian; so trước–sau
  khi đổi prompt (prompt_sha đổi).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from statistics import mean, median
from typing import Any

from aico.core import Event, Ticket, TicketStatus
from aico.tickets import TicketStore

S = TicketStatus

# Lớp lý do escalate: khớp theo đoạn đầu chuỗi lý do do builder ghi (xem _escalation_reason và
# các `escalation_reason` trong orchestrator/builder.py). Thứ tự quan trọng: khớp đầu tiên thắng.
ESCALATION_CLASSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("hết ngân sách", ("hết ngân sách",)),
    ("hết hạn mức / worker", ("không worker nào", "hạn mức", "worker", "bận quá thời gian")),
    ("review lặp", ("review chưa đạt",)),
    ("kiểm thử lặp", ("kiểm thử chưa đạt",)),
    ("đẩy code / Git", ("đẩy code", "push", "git")),
    ("merge lỗi", ("merge lỗi",)),
    ("người từ chối / quá hạn", ("kế hoạch: ", "merge: ")),
    ("kế hoạch sửa nhiều vòng", ("kế hoạch sửa quá",)),
    ("viết code lỗi", ("viết code",)),
    ("review lỗi", ("review:",)),
    ("phân tích / kế hoạch lỗi", ("phân tích yêu cầu", "lập kế hoạch")),
)


def classify_job_error(job: Mapping[str, Any]) -> str:
    """Lớp lỗi của một job (theo thông báo của worker / CliAgentRunner)."""
    if job.get("limited"):
        return "hết hạn mức"
    text = str(job.get("error") or "").lower()
    if "treo" in text or "không hoạt động" in text:
        return "agent treo"
    if "hết thời gian" in text or "timeout" in text:
        return "hết thời gian"
    if "không kết nối" in text or "http 5" in text:
        return "worker không trả lời"
    if "workspace" in text:
        return "chuẩn bị workspace"
    return "khác"


def classify_escalation(reason: str) -> str:
    text = reason.strip().lower()
    if not text:
        return "không rõ"
    for name, needles in ESCALATION_CLASSES:
        if any(n in text for n in needles):
            return name
    return "khác"


def ticket_metrics(ticket: Ticket, events: list[Event]) -> dict[str, Any]:
    """Số liệu của một ticket từ event (không gọi model)."""
    code_rounds = 0
    review_rounds = 0
    owner_returns = 0
    merge_reviews = 0
    plan_reviews = 0
    escalation = ""
    waiting = None  # trạng thái chờ người gần nhất: biết quyết định thuộc bước nào
    reached_merge_at: datetime | None = None
    jobs: list[dict[str, Any]] = []
    for e in events:
        data = e.data or {}
        if e.kind == "status":
            if e.to_status is S.IN_PROGRESS:
                code_rounds += 1
            elif e.to_status is S.IN_REVIEW:
                review_rounds += 1
            elif e.to_status is S.AWAITING_MERGE_APPROVAL and reached_merge_at is None:
                reached_merge_at = e.at
            elif e.to_status is S.ESCALATED:
                escalation = str(data.get("reason") or "")
            elif e.to_status is S.FAILED:
                escalation = f"lỗi: {data.get('error') or 'không rõ'}"
            if e.to_status in (S.AWAITING_MERGE_APPROVAL, S.AWAITING_PLAN_APPROVAL):
                waiting = e.to_status
        elif e.kind == "resume_requested":
            decision = str(data.get("decision") or "")
            if waiting is S.AWAITING_MERGE_APPROVAL:
                merge_reviews += 1
                if decision == "changes_requested":
                    owner_returns += 1
            elif waiting is S.AWAITING_PLAN_APPROVAL:
                plan_reviews += 1
        elif e.kind == "job":
            jobs.append(dict(data))
    minutes_to_merge = (
        round((reached_merge_at - ticket.created_at).total_seconds() / 60, 1)
        if reached_merge_at is not None
        else None
    )
    return {
        "id": ticket.id,
        "project": ticket.project,
        "labels": [label.lower() for label in ticket.labels],
        "status": ticket.status.value,
        "risk": ticket.risk.value if ticket.risk else None,
        "code_rounds": code_rounds,
        "review_rounds": review_rounds,
        "plan_reviews": plan_reviews,
        "merge_reviews": merge_reviews,
        "owner_returns": owner_returns,
        "escalation": escalation,
        "escalation_class": classify_escalation(escalation) if escalation else None,
        "minutes_to_merge_approval": minutes_to_merge,
        "jobs": jobs,
    }


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


def _avg(values: Iterable[float]) -> float | None:
    items = list(values)
    return round(mean(items), 2) if items else None


def _job_stats(jobs: list[Mapping[str, Any]]) -> dict[str, Any]:
    ok = [j for j in jobs if j.get("ok")]
    durations = [float(j["duration_seconds"]) for j in ok if j.get("duration_seconds") is not None]
    tokens = [
        int(j.get("tokens_in") or 0) + int(j.get("tokens_out") or 0)
        for j in ok
        if j.get("tokens_in") is not None
    ]
    errors = Counter(classify_job_error(j) for j in jobs if not j.get("ok"))
    return {
        "jobs": len(jobs),
        "ok_rate": _ratio(len(ok), len(jobs)),
        "limited": sum(1 for j in jobs if j.get("limited")),
        "errors": dict(errors.most_common()),
        "median_seconds": round(median(durations), 1) if durations else None,
        "tokens": sum(tokens),
        "avg_tokens": _avg(tokens),
    }


def _group(jobs: list[Mapping[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    by: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for j in jobs:
        by[str(j.get(key) or "?")].append(j)
    return {k: _job_stats(v) for k, v in sorted(by.items())}


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Gộp số liệu nhiều ticket. Ticket chưa kết thúc chỉ tính vào số job, không vào tỷ lệ."""
    finished = [r for r in rows if r["status"] in ("done", "failed", "escalated")]
    done = [r for r in finished if r["status"] == "done"]
    merge_reviews = sum(r["merge_reviews"] for r in rows)
    jobs = [j for r in rows for j in r["jobs"]]
    knowledge_jobs = [j for j in jobs if j.get("knowledge")]
    return {
        "tickets": len(rows),
        "finished": len(finished),
        "done": len(done),
        "escalated": sum(1 for r in finished if r["status"] == "escalated"),
        "failed": sum(1 for r in finished if r["status"] == "failed"),
        "first_pass_rate": _ratio(sum(1 for r in done if r["code_rounds"] <= 1), len(done)),
        "avg_review_rounds": _avg(r["review_rounds"] for r in finished if r["review_rounds"]),
        "owner_return_rate": _ratio(sum(r["owner_returns"] for r in rows), merge_reviews),
        "avg_minutes_to_merge_approval": _avg(
            r["minutes_to_merge_approval"]
            for r in rows
            if r["minutes_to_merge_approval"] is not None
        ),
        "escalations": dict(
            Counter(r["escalation_class"] for r in rows if r["escalation_class"]).most_common()
        ),
        "jobs": _job_stats(jobs),
        "jobs_by_role": _group(jobs, "role"),
        "jobs_by_room": _group(jobs, "room"),
        "jobs_by_prompt": _group(
            [{**j, "rp": f"{j.get('role')}@{j.get('prompt_sha')}"} for j in jobs], "rp"
        ),
        "knowledge_jobs": len(knowledge_jobs),
    }


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Tổng và tách theo nhãn chính, repo."""
    by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_label[r["labels"][0] if r["labels"] else "(không nhãn)"].append(r)
        by_project[r["project"] or "?"].append(r)
    return {
        "all": aggregate(rows),
        "by_label": {k: _short(aggregate(v)) for k, v in sorted(by_label.items())},
        "by_project": {k: _short(aggregate(v)) for k, v in sorted(by_project.items())},
    }


_SHORT_KEYS = (
    "tickets",
    "finished",
    "done",
    "escalated",
    "first_pass_rate",
    "avg_review_rounds",
    "owner_return_rate",
    "escalations",
)


def _short(agg: Mapping[str, Any]) -> dict[str, Any]:
    return {k: agg[k] for k in _SHORT_KEYS}


async def collect(store: TicketStore, *, since: datetime) -> list[dict[str, Any]]:
    """Số liệu các ticket tạo từ `since` (cũ nhất trước)."""
    picked = [t for t in await store.list() if t.created_at >= since]
    return [ticket_metrics(t, await store.events(t.id)) for t in picked]


async def report(store: TicketStore, *, now: datetime, days: int = 30) -> dict[str, Any]:
    rows = await collect(store, since=now - timedelta(days=days))
    return {"days": days, "generated_at": now.isoformat(), **metrics(rows)}
