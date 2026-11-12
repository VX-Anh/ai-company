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
    assert company_sha("abc123abc123", " 8f9b4f9c0ff\n") == "8f9b4f9c0ff-abc123abc123"
    # Mốc bẩn của aico-ops ("<git sha 11>x<băm 8>", 20 ký tự) giữ nguyên; dài quá 24 thì cắt.
    assert (
        company_sha("abc123abc123", "8F9B4F9C0FFx1a2b3c4d") == "8f9b4f9c0ffx1a2b3c4d-abc123abc123"
    )
    assert company_sha("abc", "0123456789" * 4) == "012345678901234567890123-abc"
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
    assert sha_matches(NEW, " DEADBEEF\n")  # dán kèm khoảng trắng, chữ hoa
    assert not sha_matches(NEW, "dead")  # đoạn quá ngắn dễ khớp nhầm
    assert not sha_matches(None, "deadbeef") and not sha_matches(OLD, "bbbbbbb")
    assert not sha_matches(NEW, "") and not sha_matches(NEW, None)


def test_sha_matches_long_pasted_sha_and_keeps_dirty_builds_apart() -> None:
    full = "8f9b4f9c0ff" + "e" * 29  # git sha đủ 40 ký tự
    clean, dirty, dirty2 = (f"8f9b4f9c0ff{tail}-{OLD}" for tail in ("", "x1a2b3c4d", "x99999999"))
    # Sha người dùng dán dài hơn mốc 11 ký tự vẫn khớp; sha của commit khác thì không.
    assert sha_matches(clean, full) and sha_matches(clean, full[:20])
    assert sha_matches(clean, "8f9b4f9") and not sha_matches(clean, "8f9b4f9c0f0" + "e" * 29)
    assert not sha_matches(f"8f9b4f-{OLD}", full)  # mốc lưu quá ngắn: không đủ 7 ký tự chung
    # Sha sạch không khớp bản deploy bẩn của cùng commit, và ngược lại.
    assert not sha_matches(dirty, "8f9b4f9") and not sha_matches(dirty, full)
    assert not sha_matches(clean, "8f9b4f9c0ffx") and not sha_matches(clean, "8f9b4f9x1a2b")
    # Bản bẩn: gõ "<sha>x<băm>" (đoạn đầu của băm; trống = mọi bản bẩn của commit), hai bản bẩn
    # khác băm là hai cấu hình.
    assert sha_matches(dirty, "8f9b4f9c0ffx1a2b3c4d") and sha_matches(dirty, "8f9b4f9x1a2")
    assert sha_matches(dirty, full + "x1a2b3c4d")
    assert sha_matches(dirty, "8f9b4f9x") and sha_matches(dirty2, "8f9b4f9x")
    assert not sha_matches(dirty2, "8f9b4f9x1a2") and not sha_matches(dirty, "8f9b4f9x1a2b3c4d5")
    # Dán cả mốc "<deploy>-<vân tay>": chỉ khớp trọn, không khớp theo đoạn đầu.
    assert sha_matches(dirty, f"8f9b4f9c0ffx1a2b3c4d-{OLD}")
    assert not sha_matches(dirty, f"8f9b4f9c0ff-{OLD}") and not sha_matches(clean, f"8f9b4f9-{OLD}")
    # Dấu vân tay vẫn khớp theo đoạn đầu dù bản deploy sạch hay bẩn; chỉ có một phần thì thử cả hai.
    assert sha_matches(clean, OLD[:7]) and sha_matches(dirty, OLD)
    assert sha_matches("8f9b4f9c0ff", full) and sha_matches(OLD, OLD[:8])


def row(
    i: int,
    sha: str | None,
    *,
    code_rounds: int = 1,
    review_rounds: int = 1,
    status: str = "done",
    labels: tuple[str, ...] = ("ui",),
    tokens: int = 1000,
    later_sha: str | None = None,
) -> dict[str, Any]:
    """later_sha: job cuối chạy bằng cấu hình khác (ticket vắt qua một lần deploy)."""
    shas = [sha] if later_sha is None else [sha, later_sha]
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
        "company_shas": shas,
        "last_company_sha": shas[-1],
        "started_at": (T0 + timedelta(hours=i)).isoformat(),
        "last_job_at": (T0 + timedelta(hours=i, minutes=30)).isoformat(),
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
    assert canary(rows, sha="ccccccccc")["reasons"] == ["không ticket nào chạy trọn bằng ccccccccc"]
    empty = canary([row(1, None)])
    assert empty["sha"] is None and empty["regressed"] is None
    # code_only=False: ticket research cũng vào nhóm.
    mixed = [row(0, OLD), row(1, NEW, labels=("research",))]
    assert canary(mixed, sha=NEW, n=1)["after"]["tickets"] == 0
    assert canary(mixed, sha=NEW, n=1, code_only=False)["after"]["ids"] == ["T-1"]


def test_all_escalated_or_failed_after_group_is_never_reported_as_fine() -> None:
    before = [row(i, OLD) for i in range(4)]
    # Cấu hình mới làm mọi ticket hỏng trước lần review đầu: không tỷ lệ đạt vòng đầu, không vòng
    # review nào để so. Trước đây hai vế None bị bỏ qua và kết quả là regressed=False.
    broken = [
        row(10 + i, NEW, status="failed" if i % 2 else "escalated", review_rounds=0, code_rounds=0)
        for i in range(4)
    ]
    out = canary([*before, *broken], n=4)
    assert out["after"]["first_pass_rate"] is None and out["after"]["avg_review_rounds"] is None
    assert (out["before"]["non_done_rate"], out["after"]["non_done_rate"]) == (0.0, 1.0)
    assert out["complete"] is True and out["regressed"] is True
    assert out["non_done_increase"] == 100.0
    assert "không-done" in out["reasons"][0] and "không ticket nào done" in out["reasons"][1]
    # Nhóm trước vốn đã hỏng gần hết: mức tăng dưới ngưỡng, nhưng nhóm sau không còn ticket done.
    weak = [row(i, OLD, status="done" if i == 0 else "escalated") for i in range(10)]
    none_done = [row(20 + i, NEW, status="escalated") for i in range(10)]
    out = canary([*weak, *none_done], n=10)
    assert out["non_done_increase"] == 10.0 and out["regressed"] is True
    assert len(out["reasons"]) == 1 and "không ticket nào done" in out["reasons"][0]


def test_non_done_threshold_is_a_parameter_and_missing_data_is_not_false() -> None:
    before = [row(i, OLD) for i in range(4)]
    one_bad = [row(10 + i, NEW, status="escalated" if i == 0 else "done") for i in range(4)]
    out = canary([*before, *one_bad], n=4)
    assert out["thresholds"]["non_done_increase"] == 15.0 and out["non_done_increase"] == 25.0
    assert out["regressed"] is True and out["first_pass_drop"] == 0.0
    assert canary([*before, *one_bad], n=4, non_done_increase=30)["regressed"] is False
    # Cả hai nhóm không ticket nào done: không tiêu chí nào so được -> None, không phải False.
    dead = [row(i, OLD, status="failed", review_rounds=0) for i in range(2)]
    still_dead = [row(10 + i, NEW, status="failed", review_rounds=0) for i in range(2)]
    out = canary([*dead, *still_dead], n=2)
    assert out["complete"] is True and out["non_done_increase"] == 0.0
    assert out["regressed"] is None and "thiếu dữ liệu để so" in out["reasons"][-1]
    # Nhóm trước không có ticket done nhưng nhóm sau có: thiếu vế để so tỷ lệ đạt vòng đầu.
    recovered = [row(10 + i, NEW) for i in range(2)]
    out = canary([*dead, *recovered], n=2)
    assert out["first_pass_drop"] is None and out["regressed"] is None
    # Nhóm trước chưa ticket nào kết thúc: chưa đủ mẫu.
    running = [row(i, OLD, status="in_review") for i in range(2)]
    out = canary([*running, *recovered], n=2)
    assert out["regressed"] is None and out["before"]["pending"] == ["T-0", "T-1"]
    assert "nhóm trước 0 ticket đã kết thúc" in out["reasons"][-1]


def test_groups_are_picked_by_start_order_not_by_finish() -> None:
    before = [row(0, OLD), row(1, OLD, status="awaiting_merge_approval"), row(2, OLD), row(3, OLD)]
    # Ba ticket đầu của đợt mới: một kẹt ở review, một chờ duyệt merge; hai ticket dễ bắt đầu sau
    # đã xong. Trước đây hai ticket dễ lấp chỗ và canary báo "đủ mẫu, không xấu đi".
    after = [
        row(10, NEW),
        row(11, NEW, status="in_review", code_rounds=3, review_rounds=3),
        row(12, NEW, status="awaiting_merge_approval"),
        row(13, NEW),
        row(14, NEW),
    ]
    out = canary([*before, *after], n=3)
    assert out["after"]["ids"] == ["T-10", "T-11", "T-12"]
    assert out["after"]["pending"] == ["T-11", "T-12"] and out["after"]["finished"] == 1
    assert out["complete"] is False and out["regressed"] is None
    assert "1/3 ticket đầu đã kết thúc, còn chờ T-11 T-12" in out["reasons"][-1]
    # Nhóm trước: 3 ticket BẮT ĐẦU ngay trước đợt, ticket chưa kết thúc được nêu chứ không bị
    # thay bằng ticket cũ hơn.
    assert out["before"]["ids"] == ["T-1", "T-2", "T-3"] and out["before"]["pending"] == ["T-1"]
    assert out["before"]["finished"] == 2
    # Khi cả ba ticket đầu kết thúc thì mới kết luận, và vẫn chỉ trên ba ticket đó.
    after[1] = row(11, NEW, status="escalated", code_rounds=3, review_rounds=3)
    after[2] = row(12, NEW)
    out = canary([*before, *after], n=3)
    assert out["complete"] is True and out["after"]["ids"] == ["T-10", "T-11", "T-12"]
    assert out["after"]["pending"] == [] and out["regressed"] is True


def test_tickets_straddling_a_deploy_are_left_out_of_both_groups() -> None:
    rows = [
        row(0, OLD),
        row(1, OLD),
        row(2, OLD, later_sha=NEW, code_rounds=4, review_rounds=4),  # bắt đầu cũ, xong bằng mới
        row(3, None, later_sha=NEW),  # job đầu thiếu trường (event cũ), job sau có
        row(10, NEW),
        row(11, NEW),
    ]
    out = canary(rows, n=2)
    assert out["sha"] == NEW
    assert out["before"]["ids"] == ["T-0", "T-1"] and out["after"]["ids"] == ["T-10", "T-11"]
    assert out["mixed"] == {"tickets": 2, "ids": ["T-2", "T-3"]}
    assert out["regressed"] is False  # T-2 (4 vòng) không kéo nhóm nào
    # Đợt mới chỉ mới có ticket vắt qua deploy: cấu hình mới nhất vẫn là NEW (theo job cuối),
    # báo rõ chưa có ticket chạy trọn thay vì lặng lẽ báo cáo đợt cũ.
    out = canary(rows[:4], n=2)
    assert out["sha"] == NEW and out["regressed"] is None and out["complete"] is False
    assert out["mixed"] == {"tickets": 2, "ids": ["T-2", "T-3"]}
    assert out["reasons"] == [
        f"không ticket nào chạy trọn bằng {NEW} (2 ticket vắt qua lần deploy bị loại)"
    ]
    # Ticket vắt qua một lần deploy cũ hơn cửa sổ đang so thì không đếm.
    old_mix = row(-5, "cccccccccccc", later_sha=OLD)
    assert canary([old_mix, *rows], n=2)["mixed"]["tickets"] == 2


async def _run_ticket(
    store: InMemoryTicketStore, tid: str, sha: str | None, *, later: str | None = None, **kw: Any
) -> None:
    await store.create(Ticket(id=tid, title=tid, project="x/app", **kw), actor="t")
    for to in (S.TRIAGED, S.PLANNED, S.IN_PROGRESS):
        await store.transition(tid, to, actor="test")
    data: dict[str, Any] = {"role": "coder", "room": "claude", "ok": True, "tokens_in": 10}
    if sha is not None:
        data["company_sha"] = sha
    await store.record(tid, "job", actor="coder", data=data)
    # later: job sau đó chạy bằng cấu hình khác (deploy giữa chừng).
    later_data = data if later is None else {**data, "company_sha": later}
    await store.record(tid, "job", actor="reviewer", data=later_data)
    for to in (S.IN_REVIEW, S.AWAITING_MERGE_APPROVAL, S.MERGING, S.DONE):
        await store.transition(tid, to, actor="test")


async def test_canary_api_reads_events_and_skips_eval_tickets() -> None:
    store = InMemoryTicketStore()
    await _run_ticket(store, "T-0", OLD, later=NEW)  # vắt qua lần deploy: không vào nhóm nào
    await _run_ticket(store, "T-1", None)
    await _run_ticket(store, "T-2", NEW)
    eval_art = [Artifact(kind="eval", ref="baseline@T-1")]
    await _run_ticket(store, "T-3", NEW, labels=["eval"], artifacts=eval_art)
    row2 = ticket_metrics(await store.get("T-2"), await store.events("T-2"))
    assert row2["company_sha"] == NEW and row2["started_at"] and row2["last_job_at"]
    assert row2["company_shas"] == [NEW] and row2["last_company_sha"] == NEW
    row0 = ticket_metrics(await store.get("T-0"), await store.events("T-0"))
    assert row0["company_shas"] == [OLD, NEW] and row0["last_company_sha"] == NEW
    out = await canary_report(store, now=utcnow(), n=1)
    assert out["sha"] == NEW and out["after"]["ids"] == ["T-2"] and out["before"]["ids"] == ["T-1"]
    assert out["regressed"] is False and out["mixed"] == {"tickets": 1, "ids": ["T-0"]}
    app = create_api_app(ApiDeps(tickets=store, approvals=InMemoryApprovalStore()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        body = (await c.get("/rnd/canary", params={"sha": "deadbeef", "n": 1})).json()
        assert body["after"]["ids"] == ["T-2"] and body["thresholds"]["first_pass_drop"] == 15.0
        assert body["thresholds"]["non_done_increase"] == 15.0
        # Sha dán đủ 40 ký tự (dài hơn mốc lưu) và ngưỡng không-done là tham số.
        long_sha = {"sha": "deadbeef0001" + "0" * 28, "non_done_increase": 40}
        body = (await c.get("/rnd/canary", params={**long_sha, "n": 1})).json()
        assert body["after"]["ids"] == ["T-2"] and body["thresholds"]["non_done_increase"] == 40
        assert (await c.get("/rnd/canary", params={"sha": "dead"})).status_code == 422
        assert (await c.get("/rnd/canary", params={"n": 0})).status_code == 422
