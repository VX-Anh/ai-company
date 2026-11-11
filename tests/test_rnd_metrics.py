"""Chỉ số hiệu năng hệ agent (Phòng R&D, R0): tính từ event, không gọi model."""

from __future__ import annotations

from datetime import timedelta

import httpx

from aico.api import ApiDeps, create_api_app
from aico.core import Ticket, TicketStatus, utcnow
from aico.governance import InMemoryApprovalStore
from aico.rnd.metrics import aggregate, classify_escalation, report, ticket_metrics
from aico.tickets import InMemoryTicketStore

S = TicketStatus


def job(role: str, room: str, *, ok: bool = True, sha: str = "aaaaaaaaaaaa", **extra: object):
    return {
        "role": role,
        "room": room,
        "prompt_sha": sha,
        "ok": ok,
        "tokens_in": 1000 if ok else None,
        "tokens_out": 200 if ok else None,
        "duration_seconds": 60.0 if ok else None,
        **extra,
    }


async def _walk(store: InMemoryTicketStore, tid: str, steps: list[S]) -> None:
    for to in steps:
        await store.transition(tid, to, actor="test")


async def _ticket_returned_by_owner(store: InMemoryTicketStore) -> None:
    """Code 2 lần vì review, người duyệt trả lại 1 lần sau khi Reviewer cho đạt, rồi merge."""
    await store.create(Ticket(id="T-1", title="a", project="x/app", labels=["ui"]), actor="t")
    await _walk(store, "T-1", [S.TRIAGED, S.PLANNED, S.IN_PROGRESS, S.IN_REVIEW, S.IN_PROGRESS])
    await store.record("T-1", "job", actor="coder", data=job("coder", "claude"))
    await _walk(store, "T-1", [S.IN_REVIEW, S.AWAITING_MERGE_APPROVAL])
    await store.set_pending_resume("T-1", {"decision": "changes_requested"}, actor="owner")
    await _walk(store, "T-1", [S.IN_PROGRESS, S.IN_REVIEW, S.AWAITING_MERGE_APPROVAL])
    await store.set_pending_resume("T-1", {"decision": "approved"}, actor="owner")
    await _walk(store, "T-1", [S.MERGING, S.DONE])


async def test_ticket_metrics_counts_rounds_and_owner_returns() -> None:
    store = InMemoryTicketStore()
    await _ticket_returned_by_owner(store)
    row = ticket_metrics(await store.get("T-1"), await store.events("T-1"))
    assert (row["code_rounds"], row["review_rounds"]) == (3, 3)
    assert (row["merge_reviews"], row["owner_returns"]) == (2, 1)
    assert row["escalation_class"] is None and row["minutes_to_merge_approval"] is not None
    assert [j["room"] for j in row["jobs"]] == ["claude"]


async def test_aggregate_rates_and_job_groups() -> None:
    store = InMemoryTicketStore()
    await _ticket_returned_by_owner(store)
    # Ticket đạt ngay vòng đầu.
    await store.create(Ticket(id="T-2", title="b", project="x/app"), actor="t")
    await _walk(store, "T-2", [S.TRIAGED, S.PLANNED, S.IN_PROGRESS, S.IN_REVIEW])
    await store.record("T-2", "job", actor="coder", data=job("coder", "codex", sha="bbbbbbbbbbbb"))
    await store.record("T-2", "job", actor="coder", data=job("coder", "cursor", ok=False))
    await _walk(store, "T-2", [S.AWAITING_MERGE_APPROVAL])
    await store.set_pending_resume("T-2", {"decision": "approved"}, actor="owner")
    await _walk(store, "T-2", [S.MERGING, S.DONE])
    # Ticket escalate vì review lặp.
    await store.create(Ticket(id="T-3", title="c", project="x/game"), actor="t")
    await _walk(store, "T-3", [S.TRIAGED, S.PLANNED, S.IN_PROGRESS, S.IN_REVIEW])
    await store.transition(
        "T-3", S.ESCALATED, actor="t", data={"reason": "review chưa đạt sau 3 vòng"}
    )

    rows = [
        ticket_metrics(await store.get(t), await store.events(t)) for t in ("T-1", "T-2", "T-3")
    ]
    agg = aggregate(rows)
    assert (agg["finished"], agg["done"], agg["escalated"]) == (3, 2, 1)
    assert agg["first_pass_rate"] == 0.5  # T-2 đạt ngay, T-1 phải sửa
    assert agg["owner_return_rate"] == round(1 / 3, 3)  # 1 trả lại / 3 lần duyệt merge
    assert agg["escalations"] == {"review lặp": 1}
    assert agg["jobs"]["jobs"] == 3 and agg["jobs"]["ok_rate"] == round(2 / 3, 3)
    assert agg["jobs_by_room"]["cursor"]["ok_rate"] == 0.0
    assert agg["jobs_by_room"]["codex"]["tokens"] == 1200
    assert set(agg["jobs_by_prompt"]) == {"coder@aaaaaaaaaaaa", "coder@bbbbbbbbbbbb"}


def test_classify_escalation_reasons() -> None:
    assert classify_escalation("review chưa đạt sau 5 vòng") == "review lặp"
    assert classify_escalation("viết code hết ngân sách: x") == "hết ngân sách"
    assert (
        classify_escalation("lập kế hoạch: không worker nào làm được: y") == "hết hạn mức / worker"
    )
    assert classify_escalation("đẩy code lên Git lỗi (lần code 2): 409") == "đẩy code / Git"
    assert classify_escalation("merge: timeout") == "người từ chối / quá hạn"
    assert classify_escalation("") == "không rõ"
    assert classify_escalation("chuyện lạ") == "khác"


async def test_metrics_api_reads_only_recent_tickets() -> None:
    store = InMemoryTicketStore()
    await _ticket_returned_by_owner(store)
    old = Ticket(id="T-old", title="cũ", created_at=utcnow() - timedelta(days=90))
    await store.create(old, actor="t")
    data = await report(store, now=utcnow(), days=30)
    assert data["all"]["tickets"] == 1 and "ui" in data["by_label"]
    app = create_api_app(ApiDeps(tickets=store, approvals=InMemoryApprovalStore()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        body = (await c.get("/rnd/metrics", params={"days": 120})).json()
        assert body["all"]["tickets"] == 2 and body["days"] == 120
        assert (await c.get("/rnd/metrics", params={"days": 0})).status_code == 422
