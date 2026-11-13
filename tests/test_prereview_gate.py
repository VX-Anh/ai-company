"""Cổng trước review: kiểm tất định nhánh ai/<ticket> giữa bước code và review."""

from __future__ import annotations

import base64
import io
import json
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from aico.core import Artifact, Ticket, TicketStatus
from aico.core.specs import PrereviewGateSpec
from aico.orchestrator import ReviewResult, ScriptedReviewer
from aico.orchestrator.ports import NO_CHANGE_ERROR, CodeResult, FileChange, ItemResult
from aico.orchestrator.prereview import (
    DELETIONS,
    OUTSIDE_OWNS,
    UNFINISHED_ITEMS,
    BranchDiff,
    branch_diff,
    check_branch,
    incidental,
    mentioned,
    render_feedback,
)
from aico.orchestrator.workplan import WorkItem
from aico.registry import Company
from aico.rnd.metrics import aggregate, classify_escalation, ticket_metrics
from harness import BASE_SHA, PROJECT, Harness, make_harness, seed_base, text_files

S = TicketStatus
Step = Callable[[list[WorkItem], str | None], list[ItemResult]]


def _plan(items: list[dict[str, Any]]) -> str:
    return "## Kế hoạch\nSửa nút.\n\n```json\n" + json.dumps({"items": items}) + "\n```\n"


@dataclass
class _Planner:
    text: str

    async def plan(self, ticket: Ticket, feedback: str | None, requirements: str = "") -> str:
        return self.text


@dataclass
class _Coder:
    """ItemCoder giả: mỗi lần code trả kết quả của bước kế tiếp trong kịch bản (bước cuối lặp lại).

    Không lọc theo owns như CliCoder: mô phỏng thay đổi lọt tới nhánh (vòng sửa được sửa mọi file).
    """

    steps: list[Step]
    calls: list[tuple[list[str], str | None]] = field(default_factory=list)

    async def code(self, ticket: Ticket, plan: str, feedback: str | None) -> CodeResult:
        raise AssertionError("graph phải gọi run_items")

    async def run_items(
        self, ticket: Ticket, plan: str, items: list[WorkItem], feedback: str | None
    ) -> list[ItemResult]:
        step = self.steps[min(len(self.calls), len(self.steps) - 1)]
        self.calls.append(([i.id for i in items], feedback))
        return step(items, feedback)


def _ok(item: WorkItem, *changes: FileChange) -> ItemResult:
    return ItemResult(item.id, item.title, "xong", changes=changes, summary=f"làm {item.id}")


def _edit(path: str, content: str = "mới\n") -> FileChange:
    return FileChange(path, content, "update")


def _delete(path: str) -> FileChange:
    return FileChange(path, action="delete")


FILES = {
    "src/a.ts": "a1\n",
    "src/b.ts": "b1\n",
    "src/core/alchemy/mix.ts": "mix\n",
    "art/old.json": "{}\n",
}
OWNS_A = [{"id": "W1", "title": "Sửa a", "owns": ["src/a.ts"]}]


BLOCK = PrereviewGateSpec(deletions="block", outside_owns="block", unfinished_items="block")


def _harness(
    company: Company,
    coder: _Coder,
    plan: str,
    *,
    reviews: list[ReviewResult] | None = None,
    **kw: Any,
) -> Harness:
    kw.setdefault("prereview_gate", BLOCK)  # mặc định của SOP là warn; test luồng chặn bật block
    h = make_harness(
        company,
        planner=lambda gw: _Planner(plan),
        coder=lambda gw: coder,
        reviewer=ScriptedReviewer(results=list(reviews or [])),
        **kw,
    )
    repo = h.gitlab.project(PROJECT)
    repo.files["main"].update(FILES)
    seed_base(h.gitlab)
    repo.snapshots[BASE_SHA] = dict(repo.files["main"])  # mã nguồn tại điểm tách nhánh
    return h


async def _run(h: Harness, **ticket: Any) -> Ticket:
    await h.new_ticket(**{"labels": ["small-fix"], **ticket})  # rủi ro thấp: bỏ duyệt kế hoạch
    await h.runner.run_until_idle()
    return await h.tickets.get("T-1")


async def _gates(h: Harness) -> list[dict[str, Any]]:
    return [e.data for e in await h.tickets.events("T-1") if e.kind == "gate"]


async def test_valid_diff_goes_straight_to_review(company: Company) -> None:
    coder = _Coder([lambda items, fb: [_ok(items[0], _edit("src/a.ts"))]])
    h = _harness(company, coder, _plan(OWNS_A))
    assert (await _run(h)).status is S.AWAITING_MERGE_APPROVAL
    assert len(coder.calls) == 1 and len(h.reviewer.requirements_seen) == 1
    (gate,) = await _gates(h)
    # Tài liệu kế hoạch do hệ thống ghi (docs/plans/T-1.md) không bị tính là ngoài owns.
    assert gate["passed"] and gate["violations"] == [] and gate["stale_base"] is False


async def test_deletion_outside_owns_returns_to_code_with_feedback(company: Company) -> None:
    coder = _Coder(
        [
            lambda items, fb: [
                _ok(items[0], _edit("src/a.ts"), _delete("src/core/alchemy/mix.ts"))
            ],
            lambda items, fb: [_ok(items[0], FileChange("src/core/alchemy/mix.ts", "mix\n"))],
        ]
    )
    h = _harness(company, coder, _plan(OWNS_A))
    assert (await _run(h)).status is S.AWAITING_MERGE_APPROVAL
    # Lần hai là vòng sửa (đầu việc FIX) với góp ý của cổng; reviewer chỉ chạy một lần.
    assert [ids for ids, _ in coder.calls] == [["W1"], ["FIX"]]
    feedback = coder.calls[1][1] or ""
    assert "cổng trước review" in feedback and "- src/core/alchemy/mix.ts" in feedback
    assert "ref `main`" in feedback and "src/a.ts" not in feedback
    assert len(h.reviewer.requirements_seen) == 1
    first, second = await _gates(h)
    assert first["passed"] is False and first["feedback"] == feedback
    assert first["violations"] == [
        {"check": DELETIONS, "count": 1, "paths": ["src/core/alchemy/mix.ts"], "mode": "block"}
    ]
    assert second["passed"] is True
    assert text_files(h.gitlab.project(PROJECT).files["ai/T-1"])["src/core/alchemy/mix.ts"]


async def test_deletion_inside_owns_or_named_in_ticket_is_allowed(company: Company) -> None:
    plan = _plan([{"id": "W1", "title": "Dọn", "owns": ["src/a.ts", "art/"]}])
    coder = _Coder(
        [
            lambda items, fb: [
                _ok(items[0], _edit("src/a.ts"), _delete("art/old.json"), _delete("src/b.ts"))
            ]
        ]
    )
    h = _harness(company, coder, plan)
    t = await _run(h, body="Bỏ file src/b.ts không còn dùng.")
    assert t.status is S.AWAITING_MERGE_APPROVAL and len(coder.calls) == 1


async def test_edit_outside_owns_returns_to_code(company: Company) -> None:
    coder = _Coder(
        [
            lambda items, fb: [
                _ok(items[0], _edit("src/a.ts"), _edit("src/b.ts"), _edit("src/new.ts"))
            ],
            lambda items, fb: [_ok(items[0], _edit("src/b.ts", "b1\n"), _delete("src/new.ts"))],
        ]
    )
    h = _harness(company, coder, _plan(OWNS_A))
    assert (await _run(h)).status is S.AWAITING_MERGE_APPROVAL
    feedback = coder.calls[1][1] or ""
    assert "- src/b.ts" in feedback and "- src/new.ts" in feedback and "`src/a.ts`" in feedback
    first, second = await _gates(h)
    assert first["violations"] == [
        {"check": OUTSIDE_OWNS, "count": 2, "paths": ["src/b.ts", "src/new.ts"], "mode": "block"}
    ]
    assert second["passed"] is True


async def test_file_named_by_reviewer_may_be_edited_outside_owns(company: Company) -> None:
    coder = _Coder(
        [
            lambda items, fb: [_ok(items[0], _edit("src/a.ts"))],
            lambda items, fb: [_ok(items[0], _edit("src/b.ts"))],
        ]
    )
    reviews = [ReviewResult(False, "Thiếu cập nhật src/b.ts cho khớp."), ReviewResult(True, "đạt")]
    h = _harness(company, coder, _plan(OWNS_A), reviews=reviews)
    assert (await _run(h)).status is S.AWAITING_MERGE_APPROVAL
    assert len(coder.calls) == 2 and all(g["passed"] for g in await _gates(h))


async def test_unfinished_item_is_rerun_before_review(company: Company) -> None:
    plan = _plan(
        [
            {"id": "W1", "title": "Sửa a", "owns": ["src/a.ts"]},
            {"id": "W2", "title": "Sửa b", "owns": ["src/b.ts"], "depends_on": ["W1"]},
        ]
    )

    def first(items: list[WorkItem], fb: str | None) -> list[ItemResult]:
        failed = ItemResult("W2", "Sửa b", "lỗi", error="worker hết hạn mức")
        return [_ok(items[0], _edit("src/a.ts")), failed]

    def second(items: list[WorkItem], fb: str | None) -> list[ItemResult]:
        assert items[0].owns == ("src/b.ts",) and items[0].depends_on == ()
        return [_ok(items[0], _edit("src/b.ts"))]

    coder = _Coder([first, second])
    h = _harness(company, coder, plan)
    assert (await _run(h)).status is S.AWAITING_MERGE_APPROVAL
    assert [ids for ids, _ in coder.calls] == [["W1", "W2"], ["W2"]]
    assert "W2 (Sửa b): lỗi: worker hết hạn mức" in (coder.calls[1][1] or "")
    assert len(h.reviewer.requirements_seen) == 1
    first_gate, _ = await _gates(h)
    assert first_gate["violations"] == [
        {"check": UNFINISHED_ITEMS, "count": 1, "paths": ["W2"], "mode": "block"}
    ]
    doc = text_files(h.gitlab.project(PROJECT).files["ai/T-1"])["docs/plans/T-1.md"]
    assert "| W1 | Sửa a | coder | `src/a.ts` | - | xong |" in doc
    assert "| W2 | Sửa b | coder | `src/b.ts` | W1 | xong |" in doc


async def test_gate_escalates_after_max_rounds(company: Company) -> None:
    coder = _Coder([lambda items, fb: [_ok(items[0], _edit("src/a.ts"), _edit("src/b.ts"))]])
    h = _harness(company, coder, _plan(OWNS_A))
    t = await _run(h)
    assert t.status is S.ESCALATED
    # Mặc định 2 lần trả về: code chạy 3 lần, reviewer không tốn lượt nào.
    assert len(coder.calls) == 3 and h.reviewer.requirements_seen == []
    events = await h.tickets.events("T-1")
    escalated = next(e for e in events if e.to_status is S.ESCALATED)
    reason = escalated.data["reason"]
    assert reason.startswith("cổng trước review: còn vi phạm sau 2 lần")
    assert "sửa file ngoài owns: 1" in reason and "- src/b.ts" in escalated.data["gate_feedback"]
    assert classify_escalation(reason) == "cổng trước review"
    row = ticket_metrics(t, events)
    assert (row["gate_checks"], row["gate_blocks"]) == (3, 3)
    assert row["gate_violations"] == {OUTSIDE_OWNS: 3}
    agg = aggregate([row])
    assert agg["escalations"] == {"cổng trước review": 1}
    assert agg["gate"] == {
        "checks": 3,
        "blocks": 3,
        "tickets_blocked": 1,
        "stale_base": 0,
        "skipped": 0,
        "violations": {OUTSIDE_OWNS: 3},
        "would_block": {},
        "tickets_would_block": 0,
        "ci": {"runs": 0, "failed": 0, "skipped": 0, "seconds_total": 0, "seconds_avg": None},
    }


async def test_gate_round_limit_counts_consecutive_returns_only(company: Company) -> None:
    bad: Step = lambda items, fb: [_ok(items[0], _edit("src/b.ts"))]  # noqa: E731
    good: Step = lambda items, fb: [_ok(items[0], _edit("src/b.ts", "b1\n"), _edit("src/a.ts"))]  # noqa: E731
    coder = _Coder([bad, good, bad, good])
    reviews = [ReviewResult(False, "Sửa lại thông báo lỗi."), ReviewResult(True, "đạt")]
    h = _harness(company, coder, _plan(OWNS_A), reviews=reviews)
    assert (await _run(h)).status is S.AWAITING_MERGE_APPROVAL
    assert [g["round"] for g in await _gates(h)] == [0, 1, 0, 1]
    # Sau khi cổng cho qua, vòng sửa kế tiếp nhận góp ý của reviewer chứ không phải của cổng.
    assert coder.calls[2][1] == "Sửa lại thông báo lỗi."


async def test_stale_base_is_noted_and_merged_work_is_not_counted_as_deleted(
    company: Company,
) -> None:
    h: Harness

    def second(items: list[WorkItem], fb: str | None) -> list[ItemResult]:
        # Trong lúc ticket ở vòng sửa, người khác merge file mới vào main.
        repo = h.gitlab.project(PROJECT)
        repo.files["main"]["examples/shop-ai/app.py"] = "x\n"
        repo.commits["main"].append({"id": "c" * 40, "message": "việc của người khác"})
        return [_ok(items[0], _edit("src/a.ts", "a3\n"))]

    coder = _Coder([lambda items, fb: [_ok(items[0], _edit("src/a.ts"))], second])
    reviews = [ReviewResult(False, "Sửa lại."), ReviewResult(True, "đạt")]
    h = _harness(company, coder, _plan(OWNS_A), reviews=reviews)
    assert (await _run(h)).status is S.AWAITING_MERGE_APPROVAL
    first, last = await _gates(h)
    assert first["stale_base"] is False and first["notes"] == []
    # Nhánh không có examples/shop-ai/app.py nhưng không xoá nó: chỉ ghi chú gốc cũ, vẫn cho qua.
    assert last["passed"] is True and last["stale_base"] is True
    assert "gốc cũ" in last["notes"][0]
    row = ticket_metrics(await h.tickets.get("T-1"), await h.tickets.events("T-1"))
    assert (row["gate_checks"], row["gate_blocks"], row["gate_stale_base"]) == (2, 0, 1)


async def test_plan_without_owns_checks_only_unexplained_deletions(company: Company) -> None:
    def first(items: list[WorkItem], fb: str | None) -> list[ItemResult]:
        return [
            _ok(
                items[0],
                _edit("src/a.ts"),
                _edit("src/b.ts"),
                # Đổi chỗ file: xoá ở chỗ cũ, cùng nội dung ở chỗ mới.
                _delete("art/old.json"),
                FileChange("assets/old.json", "{}\n", "create"),
                _delete("src/core/alchemy/mix.ts"),
            )
        ]

    coder = _Coder(
        [first, lambda items, fb: [_ok(items[0], FileChange("src/core/alchemy/mix.ts", "mix\n"))]]
    )
    h = _harness(company, coder, "Kế hoạch không chia đầu việc.")
    assert (await _run(h)).status is S.AWAITING_MERGE_APPROVAL
    first_gate, second_gate = await _gates(h)
    assert [(v["check"], v["paths"]) for v in first_gate["violations"]] == [
        (DELETIONS, ["src/core/alchemy/mix.ts"])
    ]
    assert second_gate["passed"] is True


async def test_replayed_ticket_passes_and_feedback_does_not_reveal_it(company: Company) -> None:
    coder = _Coder(
        [
            lambda items, fb: [_ok(items[0], _edit("src/a.ts"), _delete("src/b.ts"))],
            lambda items, fb: [_ok(items[0], FileChange("src/b.ts", "b1\n"))],
        ]
    )
    h = _harness(company, coder, _plan(OWNS_A), eval_only=True)
    ticket = Ticket(
        id="T-1",
        title="Sửa nút",
        project=PROJECT,
        labels=["ui", "eval"],
        artifacts=[Artifact(kind="eval", ref=f"thu-prompt@{BASE_SHA}")],
    )
    await h.tickets.create(ticket, actor="eval")
    await h.runner.run_until_idle()
    assert (await h.tickets.get("T-1")).status is S.AWAITING_MERGE_APPROVAL
    first, second = await _gates(h)
    # Đích là base/<id> dựng ở đúng commit gốc: không bao giờ bị coi là gốc cũ.
    assert first["stale_base"] is False and second == {**second, "passed": True, "notes": []}
    feedback = (coder.calls[1][1] or "").lower()
    assert "- src/b.ts" in feedback and "ref `base/t-1`" in feedback
    assert "eval" not in feedback and "chạy lại" not in feedback


async def test_research_ticket_is_checked_against_label_scope(company: Company) -> None:
    def first(items: list[WorkItem], fb: str | None) -> list[ItemResult]:
        assert items[0].owns == ("cards/",)  # kế hoạch không khai owns: kẹp vào phạm vi của nhãn
        return [_ok(items[0], FileChange("cards/tool/x.md", "# x\n", "create"))]

    coder = _Coder([first])
    h = _harness(company, coder, "Viết thẻ tri thức.", label_scopes={"research": ("cards/",)})
    t = await _run(h, labels=["research", "small-fix"])
    assert t.status is S.AWAITING_MERGE_APPROVAL and len(coder.calls) == 1
    (gate,) = await _gates(h)
    assert gate["passed"] is True


async def test_gate_can_be_disabled_or_skipped_by_label(company: Company) -> None:
    def coder() -> _Coder:
        return _Coder([lambda items, fb: [_ok(items[0], _edit("src/b.ts"))]])

    off = _harness(
        company, coder(), _plan(OWNS_A), prereview_gate=BLOCK.model_copy(update={"enabled": False})
    )
    assert (await _run(off)).status is S.AWAITING_MERGE_APPROVAL
    assert await _gates(off) == []

    skip = _harness(
        company,
        coder(),
        _plan(OWNS_A),
        prereview_gate=BLOCK.model_copy(update={"skip_labels": ("legacy",)}),
    )
    assert (await _run(skip, labels=["small-fix", "Legacy"])).status is S.AWAITING_MERGE_APPROVAL
    assert await _gates(skip) == []

    one = _harness(
        company,
        coder(),
        _plan(OWNS_A),
        prereview_gate=BLOCK.model_copy(update={"outside_owns": "off"}),
    )
    assert (await _run(one)).status is S.AWAITING_MERGE_APPROVAL
    assert [g["passed"] for g in await _gates(one)] == [True]


async def test_git_failure_does_not_block_review(company: Company) -> None:
    coder = _Coder([lambda items, fb: [_ok(items[0], _edit("src/b.ts"))]])
    h = make_harness(company, planner=lambda gw: _Planner(_plan(OWNS_A)), coder=lambda gw: coder)
    # main của repo giả không có commit nào: không tìm được commit chung.
    assert (await _run(h)).status is S.AWAITING_MERGE_APPROVAL
    (gate,) = await _gates(h)
    assert gate["passed"] is True and "không lấy được diff" in gate["notes"][0]
    assert gate["diff_skipped"] is True
    row = ticket_metrics(await h.tickets.get("T-1"), await h.tickets.events("T-1"))
    assert row["gate_skipped"] == 1 and aggregate([row])["gate"]["skipped"] == 1


def test_default_gate_config(company: Company) -> None:
    cfg = company.sop("fix_ticket").prereview_gate
    # Mặc định chỉ đo (warn): owner bật block sau khi xem tỷ lệ chặn nhầm trên ticket thật.
    assert cfg.enabled is True
    assert (cfg.deletions, cfg.outside_owns, cfg.unfinished_items) == ("warn",) * 3
    assert cfg.stale_base == "warn" and cfg.max_rounds == 2 and cfg.skip_labels == ()
    # SOP thiếu khoá prereview_gate thì hành xử như file mẫu (trừ lệnh kiểm theo repo).
    assert PrereviewGateSpec() == cfg.model_copy(update={"check_projects": {}})


def test_mentioned_matches_whole_paths_and_named_directories() -> None:
    assert mentioned("src/a.ts", "Xoá `src/a.ts`.")
    assert mentioned("src/a.ts", "sửa src/a.ts, rồi chạy test")
    assert not mentioned("src/a.ts", "sửa src/a.tsx và lib/src/a.ts")
    assert mentioned("src/core/alchemy/mix.ts", "bỏ thư mục src/core/alchemy/ đi")
    assert mentioned("src/core/alchemy/mix.ts", "owns: src/core/alchemy/**")
    assert mentioned("src/core/alchemy/mix.ts", "gỡ src/core/alchemy")
    assert not mentioned("src/core/alchemy/mix.ts", "sửa src/core/alchemy/other.ts")
    # Thư mục cấp một chỉ tính khi viết kèm '/'.
    assert not mentioned("src/core/x.ts", "mã trong src cần dọn")
    assert mentioned("legacy/x.ts", "xoá legacy/ vì không còn dùng")
    assert not mentioned("legacy/x.ts", "")


def test_check_branch_rules() -> None:
    item = WorkItem("W1", "a", owns=("src/a.ts", "art/"))
    diff = BranchDiff(
        deleted=("art/x.json", "lib/gone.py", "old/name.py"),
        changed=("src/a.ts", "new/name.py", "docs/plans/T-1.md", "lib/extra.py"),
        moved=frozenset({"old/name.py"}),
    )
    result = check_branch(diff, [item], [], system_paths=("docs/plans/T-1.md",))
    assert [(v.check, v.paths) for v in result.violations] == [
        (DELETIONS, ("lib/gone.py",)),
        (OUTSIDE_OWNS, ("new/name.py", "lib/extra.py")),
    ]
    text = render_feedback(result, original_ref="main")
    assert "- lib/gone.py" in text and "- lib/extra.py" in text and "art/x.json" not in text
    # Không có diff (lỗi tool Git): chỉ còn kiểm đầu việc.
    rows = [{"id": "W1", "title": "a", "status": "xong"}, {"id": "W2", "status": "bỏ qua: x"}]
    result = check_branch(None, [item], rows)
    assert [(v.check, v.paths) for v in result.violations] == [(UNFINISHED_ITEMS, ("W2",))]
    assert result.only(UNFINISHED_ITEMS)
    # Tắt từng kiểm tra.
    assert check_branch(diff, [item], rows, deletions=False, outside_owns=False).only(
        UNFINISHED_ITEMS
    )
    assert check_branch(
        diff, [item], rows, deletions=False, outside_owns=False, unfinished_items=False
    ).passed


def test_gate_modes_from_yaml_booleans() -> None:
    # YAML đọc `off` trần thành False: phải tắt thật chứ không lỗi; `on`/true thì bắt ghi rõ mức.
    cfg = PrereviewGateSpec.model_validate({"deletions": False, "stale_base": False})
    assert (cfg.deletions, cfg.stale_base, cfg.outside_owns) == ("off", "off", "warn")
    with pytest.raises(ValueError, match="off \\| warn \\| block"):
        PrereviewGateSpec.model_validate({"outside_owns": True})


async def test_warn_mode_records_would_block_and_goes_to_review(company: Company) -> None:
    plan = _plan(
        [
            {"id": "W1", "title": "Sửa a", "owns": ["src/a.ts"]},
            {"id": "W2", "title": "Sửa b", "owns": ["src/b.ts"]},
        ]
    )

    def first(items: list[WorkItem], fb: str | None) -> list[ItemResult]:
        done = _ok(items[0], _edit("src/a.ts"), _edit("lib/x.ts"), _delete("art/old.json"))
        return [done, ItemResult("W2", "Sửa b", "lỗi", error="worker hết hạn mức")]

    coder = _Coder([first, lambda items, fb: [_ok(items[0], _edit("src/a.ts", "a3\n"))]])
    reviews = [ReviewResult(False, "Sửa lại thông báo."), ReviewResult(True, "đạt")]
    # Cấu hình thật của company/ (mặc định warn).
    h = _harness(company, coder, plan, reviews=reviews, prereview_gate=None)
    assert (await _run(h)).status is S.AWAITING_MERGE_APPROVAL
    # Không trả về, không gửi góp ý của cổng cho coder: vòng hai là vòng sửa của reviewer.
    assert [fb for _, fb in coder.calls] == [None, "Sửa lại thông báo."]
    first_gate, second_gate = await _gates(h)
    assert first_gate["passed"] is True and "feedback" not in first_gate
    assert first_gate["would_block"] == [DELETIONS, OUTSIDE_OWNS, UNFINISHED_ITEMS]
    assert [(v["check"], v["mode"], v["paths"]) for v in first_gate["violations"]] == [
        (DELETIONS, "warn", ["art/old.json"]),
        (OUTSIDE_OWNS, "warn", ["lib/x.ts"]),
        (UNFINISHED_ITEMS, "warn", ["W2"]),
    ]
    # Vi phạm về file đã ghi nhận một lần không đếm lại ở vòng sau; đầu việc dở thì vẫn còn.
    assert second_gate["would_block"] == [UNFINISHED_ITEMS]
    row = ticket_metrics(await h.tickets.get("T-1"), await h.tickets.events("T-1"))
    assert (row["gate_checks"], row["gate_blocks"], row["gate_violations"]) == (2, 0, {})
    assert row["gate_would_block"] == {DELETIONS: 1, OUTSIDE_OWNS: 1, UNFINISHED_ITEMS: 2}
    gate = aggregate([row])["gate"]
    assert gate["would_block"] == row["gate_would_block"] and gate["tickets_would_block"] == 1


async def test_earlier_review_requests_stay_allowed_in_later_rounds(company: Company) -> None:
    # C1: diff cộng dồn cả nhánh; file reviewer yêu cầu ở vòng 1 không bị đòi hoàn tác ở vòng 2, 3.
    coder = _Coder(
        [
            lambda items, fb: [_ok(items[0], _edit("src/a.ts"))],
            lambda items, fb: [_ok(items[0], _edit("src/b.ts"), _delete("art/old.json"))],
            lambda items, fb: [_ok(items[0], _edit("src/a.ts", "a3\n"))],
            lambda items, fb: [_ok(items[0], _edit("src/a.ts", "a4\n"))],
        ]
    )
    reviews = [
        ReviewResult(False, "Thiếu cập nhật src/b.ts cho khớp; bỏ art/old.json."),
        ReviewResult(False, "Thông báo lỗi còn sai chính tả."),
        ReviewResult(False, "Vẫn sai một chỗ."),
        ReviewResult(True, "đạt"),
    ]
    h = _harness(
        company, coder, _plan(OWNS_A), reviews=reviews, label_review_rounds={"small-fix": 5}
    )
    assert (await _run(h)).status is S.AWAITING_MERGE_APPROVAL
    gates = await _gates(h)
    assert len(gates) == 4 and all(g["passed"] and not g["violations"] for g in gates)
    assert all("cổng trước review" not in (fb or "") for _, fb in coder.calls)


def test_accepted_paths_are_not_rechecked() -> None:
    item = WorkItem("W1", "a", owns=("src/a.ts",))
    diff = BranchDiff(deleted=("lib/gone.py",), changed=("src/a.ts", "lib/x.py", "lib/new.py"))
    accepted = frozenset({"lib/gone.py", "lib/x.py"})
    result = check_branch(diff, [item], [], accepted=accepted)
    assert [(v.check, v.paths) for v in result.violations] == [(OUTSIDE_OWNS, ("lib/new.py",))]


async def test_ticket_from_before_the_gate_is_not_blocked_for_old_changes(
    company: Company,
) -> None:
    # Ticket đang dở lúc deploy: state không có trường nào của cổng, nhánh đã có thay đổi
    # ngoài owns từ các vòng trước.
    coder = _Coder(
        [
            lambda items, fb: [_ok(items[0], _edit("src/a.ts", "a3\n"))],
            lambda items, fb: [_ok(items[0], _edit("lib/new.ts"))],
            lambda items, fb: [_ok(items[0], _delete("lib/new.ts"))],
        ]
    )
    reviews = [ReviewResult(False, "Sửa tiếp."), ReviewResult(True, "đạt")]
    h = _harness(company, coder, _plan(OWNS_A), reviews=reviews)
    await h.new_ticket(labels=["small-fix"])
    repo = h.gitlab.project(PROJECT)
    await h.gitlab.create_branch(PROJECT, "ai/T-1", "main")
    await h.gitlab.commit(
        PROJECT,
        "ai/T-1",
        "vòng trước",
        [
            {"action": "update", "file_path": "src/b.ts", "content": "b-cũ\n"},
            {"action": "delete", "file_path": "art/old.json"},
        ],
    )
    mr = await h.gitlab.create_mr(PROJECT, "ai/T-1", "main", "T-1", "")
    graph: Any = h.runner._graph  # pyright: ignore[reportPrivateUsage]
    cfg: Any = {"configurable": {"thread_id": "T-1"}}
    old = {
        "ticket_id": "T-1",
        "risk": "low",
        "plan_rounds": 1,
        "code_attempts": 1,
        "review_rounds": 1,
        "test_rounds": 0,
        "merge_approval_rounds": 0,
        "merge_attempts": 0,
        "merged": False,
        "eval": False,
        "plan": _plan(OWNS_A),
        "work_items": [WorkItem("W1", "Sửa a", owns=("src/a.ts",)).to_dict()],
        "review_passed": False,
        "review_notes": "sửa a",
        "mr_iid": mr["iid"],
        "branch": "ai/T-1",
    }
    await graph.aupdate_state(cfg, old, as_node="plan")
    await h.tickets.transition("T-1", S.TRIAGED, actor="test")
    await h.tickets.transition("T-1", S.PLANNED, actor="test")
    await h.runner.run_until_idle()
    assert (await h.tickets.get("T-1")).status is S.AWAITING_MERGE_APPROVAL
    first, second, third = await _gates(h)
    # Lần kiểm đầu nhận hết diff đang có (src/b.ts sửa, art/old.json xoá từ vòng trước).
    assert first["passed"] is True and first["violations"] == []
    assert "từ trước khi có cổng" in first["notes"][0]
    # Thay đổi MỚI ngoài owns sau đó vẫn bị bắt.
    assert [(v["check"], v["paths"]) for v in second["violations"]] == [
        (OUTSIDE_OWNS, ["lib/new.ts"])
    ]
    assert third["passed"] is True and "art/old.json" not in repo.files["ai/T-1"]


async def test_tests_lockfiles_and_generated_siblings_are_not_outside_owns(
    company: Company,
) -> None:
    # C2: reviewer ghi chung chung ("thiếu test hồi quy"), không nêu đường dẫn.
    plan = _plan([{"id": "W1", "title": "Sprite", "owns": ["src/a.ts", "art/hero.pixel.json"]}])
    coder = _Coder(
        [
            lambda items, fb: [
                _ok(
                    items[0],
                    _edit("src/a.ts"),
                    FileChange("art/hero.pixel.json", "{}\n", "create"),
                    FileChange("art/hero.png", "png\n", "create"),  # sinh từ hero.pixel.json
                )
            ],
            lambda items, fb: [
                _ok(
                    items[0],
                    FileChange("tests/a.test.ts", "t\n", "create"),
                    FileChange("src/__snapshots__/a.snap", "s\n", "create"),
                    FileChange("package-lock.json", "{}\n", "create"),
                )
            ],
        ]
    )
    reviews = [ReviewResult(False, "Thiếu test hồi quy cho nhánh lỗi."), ReviewResult(True, "đạt")]
    h = _harness(company, coder, plan, reviews=reviews)
    assert (await _run(h)).status is S.AWAITING_MERGE_APPROVAL
    gates = await _gates(h)
    assert len(coder.calls) == 2 and [g["violations"] for g in gates] == [[], []]


def test_incidental_files() -> None:
    for path in (
        "package-lock.json",
        "web/pnpm-lock.yaml",
        "uv.lock",
        "tests/unit/x.py",
        "src/__tests__/a.ts",
        "src/a.test.tsx",
        "src/a.spec.ts",
        "pkg/test_x.py",
        "pkg/x_test.go",
        "src/__snapshots__/a.snap",
    ):
        assert incidental(path), path
    for path in ("src/b.ts", "src/testing.ts", "src/latest/x.ts", "lock.json", "contest.py"):
        assert not incidental(path), path
    owned = frozenset({"art/hero.pixel.json"})
    assert incidental("art/hero.png", owned) and not incidental("art/villain.png", owned)
    assert not incidental("other/hero.png", owned)
    # File test bị XOÁ thì không được miễn: miễn trừ chỉ áp cho sửa/thêm.
    item = WorkItem("W1", "a", owns=("src/a.ts",))
    diff = BranchDiff(deleted=("tests/a.test.ts",), changed=("tests/b.test.ts",))
    assert [(v.check, v.paths) for v in check_branch(diff, [item], []).violations] == [
        (DELETIONS, ("tests/a.test.ts",))
    ]


async def test_item_with_nothing_to_change_reaches_review_after_one_rerun(
    company: Company,
) -> None:
    # TB-1: agent thấy không cần sửa (hoặc owns sai). Chạy lại một lần, vẫn vậy thì để reviewer xét.
    plan = _plan(
        [
            {"id": "W1", "title": "Sửa a", "owns": ["src/a.ts"]},
            {"id": "W2", "title": "Kiểm b", "owns": ["src/b.ts"]},
        ]
    )

    def nothing(i: WorkItem) -> ItemResult:
        return ItemResult(i.id, i.title, "lỗi", error=NO_CHANGE_ERROR)

    coder = _Coder(
        [
            lambda items, fb: [_ok(items[0], _edit("src/a.ts")), nothing(items[1])],
            lambda items, fb: [nothing(i) for i in items],
        ]
    )
    h = _harness(company, coder, plan)
    assert (await _run(h)).status is S.AWAITING_MERGE_APPROVAL
    assert [ids for ids, _ in coder.calls] == [["W1", "W2"], ["W2"]]
    first, second = await _gates(h)
    assert first["passed"] is False
    assert second["passed"] is True and second["violations"] == []
    assert "W2 chạy lại vẫn không sửa file nào" in second["notes"][0]
    assert len(h.reviewer.requirements_seen) == 1


async def test_item_failing_for_worker_errors_still_escalates(company: Company) -> None:
    plan = _plan(
        [
            {"id": "W1", "title": "Sửa a", "owns": ["src/a.ts"]},
            {"id": "W2", "title": "Sửa b", "owns": ["src/b.ts"]},
        ]
    )

    def failed(i: WorkItem) -> ItemResult:
        return ItemResult(i.id, i.title, "lỗi", error="worker hết hạn mức")

    coder = _Coder(
        [
            lambda items, fb: [_ok(items[0], _edit("src/a.ts")), failed(items[1])],
            lambda items, fb: [failed(i) for i in items],
        ]
    )
    h = _harness(company, coder, plan)
    assert (await _run(h)).status is S.ESCALATED
    assert h.reviewer.requirements_seen == []


async def test_unfinished_item_survives_a_fix_round(company: Company) -> None:
    # TB-3: vi phạm hỗn hợp. Vòng FIX chỉ khôi phục file; đầu việc dở không được mất dấu.
    plan = _plan(
        [
            {"id": "W1", "title": "Sửa a", "owns": ["src/a.ts"]},
            {"id": "W2", "title": "Sửa b", "owns": ["src/b.ts"]},
        ]
    )
    coder = _Coder(
        [
            lambda items, fb: [
                _ok(items[0], _edit("src/a.ts"), _delete("art/old.json")),
                ItemResult("W2", "Sửa b", "lỗi", error="worker lỗi"),
            ],
            lambda items, fb: [_ok(items[0], FileChange("art/old.json", "{}\n", "create"))],
            lambda items, fb: [_ok(items[0], _edit("src/b.ts"))],
        ]
    )
    h = _harness(company, coder, plan)
    assert (await _run(h)).status is S.AWAITING_MERGE_APPROVAL
    assert [ids for ids, _ in coder.calls] == [["W1", "W2"], ["FIX"], ["W2"]]
    first, second, third = await _gates(h)
    assert [v["check"] for v in first["violations"]] == [DELETIONS, UNFINISHED_ITEMS]
    assert [v["check"] for v in second["violations"]] == [UNFINISHED_ITEMS]
    assert third["passed"] is True and len(h.reviewer.requirements_seen) == 1
    doc = text_files(h.gitlab.project(PROJECT).files["ai/T-1"])["docs/plans/T-1.md"]
    assert "| W2 | Sửa b | coder | `src/b.ts` | - | xong |" in doc


def test_move_detection_needs_same_content_or_nearby_folder() -> None:
    def tar(files: dict[str, str]) -> str:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as archive:
            for path, content in files.items():
                info = tarfile.TarInfo(f"repo/{path}")
                info.size = len(content.encode())
                archive.addfile(info, io.BytesIO(content.encode()))
        return base64.b64encode(buf.getvalue()).decode("ascii")

    base = tar(
        {"src/core/index.ts": "lõi\n", "src/a/util.ts": "u\n", "old/data.json": "{}\n", "k": "1"}
    )
    head = tar(
        {
            "docs/index.ts": "khác hẳn\n",  # cùng tên, thư mục xa, nội dung khác: không phải chuyển
            "src/b/util.ts": "u2\n",  # cùng tên, thư mục kề: chuyển chỗ có sửa
            "deep/er/place/renamed.json": "{}\n",  # cùng nội dung: chuyển chỗ
            "k": "1",
        }
    )
    diff = branch_diff(base, head)
    assert diff.deleted == ("old/data.json", "src/a/util.ts", "src/core/index.ts")
    assert diff.moved == {"old/data.json", "src/a/util.ts"}
