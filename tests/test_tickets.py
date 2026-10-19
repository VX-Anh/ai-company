from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from aico.core import Artifact, Ticket, TicketSource, TicketStatus, utcnow
from aico.tickets import TicketExists, TicketNotFound, TransitionError, can_transition

S = TicketStatus


def _ticket(ticket_id: str = "T-1", **kw: Any) -> Ticket:
    return Ticket(id=ticket_id, title="Sửa lỗi đăng nhập", **kw)


def test_state_machine_matches_design() -> None:
    assert can_transition(S.PLANNED, S.IN_PROGRESS)  # rủi ro low bỏ qua duyệt
    assert can_transition(S.PLANNED, S.AWAITING_PLAN_APPROVAL)
    assert can_transition(S.MERGING, S.DONE)
    assert can_transition(S.IN_REVIEW, S.FAILED)
    assert can_transition(S.DONE, S.DONE)  # chạy lại node là no-op
    assert not can_transition(S.NEW, S.DONE)
    assert not can_transition(S.IN_REVIEW, S.DONE)  # không merge thì không xong
    assert not can_transition(S.DONE, S.FAILED)
    assert not can_transition(S.FAILED, S.IN_PROGRESS)


async def test_create_get_and_events(store: Any) -> None:
    await store.create(_ticket(), actor="api")
    got = await store.get("T-1")
    assert got.status is S.NEW
    with pytest.raises(TicketExists):
        await store.create(_ticket(), actor="api")
    with pytest.raises(TicketNotFound):
        await store.get("nope")
    events = await store.events("T-1")
    assert [e.kind for e in events] == ["created"]


async def test_transition_records_event_and_rejects_invalid(store: Any) -> None:
    await store.create(_ticket(), actor="api")
    await store.transition("T-1", S.TRIAGED, actor="triage", data={"risk": "low"})
    again = await store.transition("T-1", S.TRIAGED, actor="triage")
    assert again.status is S.TRIAGED
    with pytest.raises(TransitionError):
        await store.transition("T-1", S.DONE, actor="x")
    events = await store.events("T-1")
    status_events = [e for e in events if e.kind == "status"]
    assert len(status_events) == 1
    assert status_events[0].from_status is S.NEW
    assert status_events[0].data == {"risk": "low"}


async def test_create_from_source_is_idempotent(store: Any) -> None:
    ref = "gitlab:group/pilot-repo#12"
    first, created = await store.create_from_source(
        _ticket("T-a", source=TicketSource.GITLAB, source_ref=ref), actor="intake"
    )
    second, created_again = await store.create_from_source(
        _ticket("T-b", source=TicketSource.GITLAB, source_ref=ref), actor="intake"
    )
    assert created and not created_again
    assert second.id == first.id == "T-a"


async def test_update_and_artifacts(store: Any) -> None:
    await store.create(_ticket(), actor="api")
    updated = await store.update("T-1", actor="triage", risk="high", department="engineering")
    assert updated.risk == "high"
    mr = Artifact(kind="merge_request", ref="group/pilot-repo!7")
    await store.add_artifact("T-1", mr, actor="coder")
    again = await store.add_artifact("T-1", mr, actor="coder")
    assert again.artifacts == [mr]
    with pytest.raises(ValueError):
        await store.update("T-1", actor="x", status="done")


async def test_lease_claim_renew_release(store: Any) -> None:
    await store.create(_ticket("T-1"), actor="api")
    await store.create(_ticket("T-2"), actor="api")
    a = await store.claim(owner="worker-a", lease_seconds=30)
    b = await store.claim(owner="worker-b", lease_seconds=30)
    assert a is not None and b is not None and a.id != b.id
    assert await store.claim(owner="worker-c", lease_seconds=30) is None
    assert await store.renew(a.id, owner="worker-a", lease_seconds=30)
    assert not await store.renew(a.id, owner="worker-b", lease_seconds=30)
    await store.release(a.id, owner="worker-a")
    again = await store.claim(owner="worker-c", lease_seconds=30)
    assert again is not None and again.id == a.id


async def test_expired_lease_can_be_reclaimed(store: Any) -> None:
    await store.create(_ticket(), actor="api")
    first = await store.claim(owner="dead-worker", lease_seconds=0.05)
    assert first is not None
    await asyncio.sleep(0.2)
    second = await store.claim(owner="new-worker", lease_seconds=30)
    assert second is not None and second.lease_owner == "new-worker"


async def test_concurrent_claims_never_share_a_ticket(store: Any) -> None:
    for i in range(5):
        await store.create(_ticket(f"T-{i}"), actor="api")
    claimed = await asyncio.gather(
        *(store.claim(owner=f"w{i}", lease_seconds=30) for i in range(10))
    )
    ids = [t.id for t in claimed if t is not None]
    assert sorted(ids) == [f"T-{i}" for i in range(5)]


async def test_waiting_ticket_is_claimable_only_with_pending_resume(store: Any) -> None:
    await store.create(_ticket(status=S.AWAITING_PLAN_APPROVAL), actor="api")
    assert await store.claim(owner="w", lease_seconds=30) is None
    await store.set_pending_resume("T-1", {"decision": "approved"}, actor="owner")
    claimed = await store.claim(owner="w", lease_seconds=30)
    assert claimed is not None
    assert await store.take_pending_resume("T-1", owner="other") is None
    assert await store.take_pending_resume("T-1", owner="w") == {"decision": "approved"}
    assert await store.take_pending_resume("T-1", owner="w") is None


async def test_overdue_approvals(store: Any) -> None:
    past = utcnow() - timedelta(hours=1)
    await store.create(
        _ticket("T-1", status=S.AWAITING_MERGE_APPROVAL, approval_deadline=past), actor="api"
    )
    await store.create(_ticket("T-2", status=S.AWAITING_MERGE_APPROVAL), actor="api")
    overdue = await store.overdue_approvals(utcnow())
    assert [t.id for t in overdue] == ["T-1"]
