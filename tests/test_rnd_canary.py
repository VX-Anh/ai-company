"""Canary (Phòng R&D, R2 mục 2.4): company_sha trên event job và so ticket trước–sau."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from aico.agents.cli_agent import company_sha, job_record
from aico.api import ApiDeps, create_api_app
from aico.core import Artifact, Ticket, TicketStatus, utcnow
from aico.governance import InMemoryApprovalStore
from aico.registry import company_fingerprint, load_company
from aico.rnd.canary import canary, canary_report, sha_matches
from aico.rnd.metrics import ticket_metrics
from aico.settings import Settings
from aico.tickets import InMemoryTicketStore

S = TicketStatus
OLD, NEW = "aaaaaaaaaaaa", "deadbeef0001-bbbbbbbbbbbb"
T0 = utcnow() - timedelta(days=5)


def test_fingerprint_is_stable_and_follows_agent_config(company_copy: Path) -> None:
    base = company_fingerprint(company_copy)
    assert len(base) == 12 and load_company(company_copy).sha == base
    # Watchlist chỉ là nguồn dò công nghệ: đổi nó không mở đợt canary mới.
    (company_copy / "rnd" / "watchlist.yaml").write_text("sources: []\n", encoding="utf-8")
    assert company_fingerprint(company_copy) == base
    # Cùng nội dung, khác kiểu xuống dòng (Windows và container) cho cùng giá trị.
    prompt = company_copy / "prompts" / "coder.md"
    text = prompt.read_bytes().replace(b"\r\n", b"\n")
    prompt.write_bytes(text.replace(b"\n", b"\r\n"))
    assert company_fingerprint(company_copy) == base
    prompt.write_bytes(text + b"\nThem mot dong.\n")
    assert company_fingerprint(company_copy) != base


def test_company_sha_joins_deploy_mark_and_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    assert company_sha("abc123abc123") == "abc123abc123"
    assert company_sha("abc123abc123", " 8f9b4f9c0ffee1234567\n") == "8f9b4f9c0ffe-abc123abc123"
    assert company_sha("") is None
    # Không truyền (Company dựng tay): trường vẫn có, giá trị None như event cũ thiếu trường.
    record = job_record(
        role_id="coder", mode="write", room="claude", system_prompt="p", outcome={}, critical=False
    )
    assert record["company_sha"] is None
    monkeypatch.setenv("AICO_DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("AICO_COMPANY_SHA", "8f9b4f9")
    assert Settings.from_env().deploy_sha == "8f9b4f9"
    monkeypatch.setenv("AICO_COMPANY_SHA", "")
    assert Settings.from_env().deploy_sha is None


def test_sha_matches_whole_or_prefix_of_either_part() -> None:
    assert sha_matches(NEW, NEW) and sha_matches(NEW, "deadbeef") and sha_matches(NEW, "bbbbbbb")
    assert not sha_matches(NEW, "dead")  # đoạn quá ngắn dễ khớp nhầm
    assert not sha_matches(None, "deadbeef") and not sha_matches(OLD, "bbbbbbb")


def row(
    i: int,
    sha: str | None,
    *,
    code_rounds: int = 1,
    review_rounds: int = 1,
    status: str = "done",
    labels: tuple[str, ...] = ("ui",),
    tokens: int = 1000,
) -> dict[str, Any]:
    return {
        "id": f"T-{i}",
        "labels": list(labels),
        "status": status,
        "code_rounds": code_rounds,
        "review_rounds": review_rounds,
        "merge_reviews": 1,
        "owner_returns": 0,
        "escalation_class": None,
        "minutes_to_merge_approval": 10.0,
        "company_sha": sha,
        "started_at": (T0 + timedelta(hours=i)).isoformat(),
        "jobs": [{"role": "coder", "ok": True, "tokens_in": tokens, "tokens_out": 0}],
    }


def test_canary_flags_first_pass_drop() -> None:
    before = [row(i, None if i < 3 else OLD) for i in range(6)]  # event cũ thiếu trường vẫn tính
    after = [row(10 + i, NEW, code_rounds=2 if i < 2 else 1, tokens=3000) for i in range(4)]
    waiting = [row(20, NEW, status="in_review"), row(21, NEW, labels=("research",))]
    out = canary([*after, *waiting, *before], sha="deadbeef", n=4)
    assert out["after"]["ids"] == ["T-10", "T-11", "T-12", "T-13"]
    assert out["before"]["ids"] == ["T-2", "T-3", "T-4", "T-5"]  # 4 ticket NGAY trước
    assert out["before"]["company_shas"] == {OLD: 3, "?": 1}
    assert (out["before"]["first_pass_rate"], out["after"]["first_pass_rate"]) == (1.0, 0.5)
    assert (out["before"]["avg_tokens"], out["after"]["avg_tokens"]) == (1000, 3000)
    assert out["first_pass_drop"] == 50.0 and out["complete"] is True
    assert out["regressed"] is True and "đạt vòng đầu giảm" in out["reasons"][0]
    # Ngưỡng là tham số.
    assert canary([*after, *before], sha=NEW, n=4, first_pass_drop=60)["regressed"] is False


def test_canary_flags_review_rounds_and_defaults_to_latest_config() -> None:
    before = [row(i, OLD, review_rounds=1) for i in range(4)]
    after = [row(10 + i, NEW, review_rounds=2 if i % 2 else 1) for i in range(4)]
    out = canary([*before, *after], n=4)
    assert out["sha"] == NEW and out["review_increase"] == 0.5 and out["first_pass_drop"] == 0.0
    assert out["regressed"] is True and "vòng review" in out["reasons"][0]
    good = canary([*before, *[row(10 + i, NEW) for i in range(4)]], n=4)
    assert good["regressed"] is False and good["reasons"] == []


def test_canary_needs_full_sample_and_uses_latest_run_of_a_config() -> None:
    rows = [row(i, OLD) for i in range(3)] + [row(5, NEW, code_rounds=3)]
    out = canary(rows, n=3)
    assert out["regressed"] is None and out["complete"] is False
    assert out["first_pass_drop"] == 100.0 and "chưa đủ mẫu" in out["reasons"][-1]
    # Cấu hình cũ quay lại sau một lần revert: chỉ lấy đợt gần nhất, nhóm trước là đợt NEW.
    rows += [row(8, OLD), row(9, OLD)]
    back = canary(rows, sha=OLD, n=2)
    assert back["after"]["ids"] == ["T-8", "T-9"] and back["before"]["ids"] == ["T-2", "T-5"]
    assert canary(rows, sha="ccccccccc")["reasons"] == ["không ticket nào chạy ccccccccc"]
    empty = canary([row(1, None)])
    assert empty["sha"] is None and empty["regressed"] is None
    # code_only=False: ticket research cũng vào nhóm.
    mixed = [row(0, OLD), row(1, NEW, labels=("research",))]
    assert canary(mixed, sha=NEW, n=1)["after"]["tickets"] == 0
    assert canary(mixed, sha=NEW, n=1, code_only=False)["after"]["ids"] == ["T-1"]


async def _run_ticket(store: InMemoryTicketStore, tid: str, sha: str | None, **kw: Any) -> None:
    await store.create(Ticket(id=tid, title=tid, project="x/app", **kw), actor="t")
    for to in (S.TRIAGED, S.PLANNED, S.IN_PROGRESS):
        await store.transition(tid, to, actor="test")
    data: dict[str, Any] = {"role": "coder", "room": "claude", "ok": True, "tokens_in": 10}
    if sha is not None:
        data["company_sha"] = sha
    await store.record(tid, "job", actor="coder", data=data)
    # Job sau đó chạy bằng cấu hình khác (deploy giữa chừng): ticket vẫn thuộc cấu hình đầu.
    await store.record(tid, "job", actor="reviewer", data={**data, "company_sha": "zzzzzzzzzzzz"})
    for to in (S.IN_REVIEW, S.AWAITING_MERGE_APPROVAL, S.MERGING, S.DONE):
        await store.transition(tid, to, actor="test")


async def test_canary_api_reads_events_and_skips_eval_tickets() -> None:
    store = InMemoryTicketStore()
    await _run_ticket(store, "T-1", None)
    await _run_ticket(store, "T-2", NEW)
    eval_art = [Artifact(kind="eval", ref="baseline@T-1")]
    await _run_ticket(store, "T-3", NEW, labels=["eval"], artifacts=eval_art)
    row2 = ticket_metrics(await store.get("T-2"), await store.events("T-2"))
    assert row2["company_sha"] == NEW and row2["started_at"]
    out = await canary_report(store, now=utcnow(), n=1)
    assert out["sha"] == NEW and out["after"]["ids"] == ["T-2"] and out["before"]["ids"] == ["T-1"]
    assert out["regressed"] is False
    app = create_api_app(ApiDeps(tickets=store, approvals=InMemoryApprovalStore()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        body = (await c.get("/rnd/canary", params={"sha": "deadbeef", "n": 1})).json()
        assert body["after"]["ids"] == ["T-2"] and body["thresholds"]["first_pass_drop"] == 15.0
        assert (await c.get("/rnd/canary", params={"sha": "dead"})).status_code == 422
        assert (await c.get("/rnd/canary", params={"n": 0})).status_code == 422
