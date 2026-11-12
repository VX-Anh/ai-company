"""Ticket chạy lại (Phòng R&D, R1): nhánh ai/<id> từ commit gốc, MR vào base/<id>, chỉ instance
eval nhận, không qua duyệt kế hoạch, không bao giờ merge vào main."""

from __future__ import annotations

from aico.core import Artifact, Ticket, TicketStatus
from aico.orchestrator.escalation import EscalationError, merge_escalated, retry_push
from aico.orchestrator.workplan import eval_spec, ticket_refs
from aico.registry import Company
from aico.rnd.metrics import collect
from harness import BASE_SHA, PROJECT, make_harness

S = TicketStatus


def eval_ticket(tid: str = "T-1", base: str = BASE_SHA) -> Ticket:
    return Ticket(
        id=tid,
        title="Chạy lại: sửa nút",
        project=PROJECT,
        labels=["ui", "eval"],
        artifacts=[Artifact(kind="eval", ref=f"thu-prompt@{base}")],
    )


def test_ticket_refs_for_normal_and_eval_tickets() -> None:
    normal = Ticket(id="T-9", title="x", labels=["eval"])  # nhãn mà thiếu artifact: như thường
    assert ticket_refs(normal) == ticket_refs(normal.model_copy(update={"labels": []}))
    assert ticket_refs(normal).work == "ai/T-9" and ticket_refs(normal).target == "main"
    refs = ticket_refs(eval_ticket(base="a" * 40))
    # Tên trung tính: agent thấy ai/<id> như ticket thường, đích base/<id>.
    assert (refs.work, refs.base, refs.target) == ("ai/T-1", "a" * 40, "base/T-1")
    assert refs.eval_variant == "thu-prompt" and eval_spec(eval_ticket()) == (
        "thu-prompt",
        BASE_SHA,
    )


async def test_eval_ticket_runs_only_on_eval_instance_and_never_targets_main(
    company: Company,
) -> None:
    h = make_harness(company)
    await h.tickets.create(eval_ticket(), actor="eval")
    # aico chính không nhận ticket chạy lại.
    await h.runner.run_until_idle()
    assert (await h.tickets.get("T-1")).status is S.NEW

    e = make_harness(company, tickets=h.tickets, eval_only=True)
    e.gitlab.project(PROJECT).files["main"]["app.py"] = "print('cũ')\n"
    await e.runner.run_until_idle()
    t = await e.tickets.get("T-1")
    # Không qua duyệt kế hoạch (luật eval = low), tới chờ duyệt merge để evaluator chấm.
    assert t.status is S.AWAITING_MERGE_APPROVAL
    statuses = [ev.to_status for ev in await e.tickets.events("T-1") if ev.kind == "status"]
    assert S.AWAITING_PLAN_APPROVAL not in statuses
    files = e.gitlab.project(PROJECT).files
    assert "ai/T-1" in files and "base/T-1" in files
    assert not any("eval" in name for name in files)
    mrs = e.gitlab.project(PROJECT).mrs
    assert [(m["source_branch"], m["target_branch"]) for m in mrs] == [("ai/T-1", "base/T-1")]
    # Ticket chạy lại không lẫn vào chỉ số của hệ thật.
    from datetime import timedelta

    since = t.created_at - timedelta(minutes=1)
    assert await collect(e.tickets, since=since) == []
    assert [r["id"] for r in await collect(e.tickets, since=since, eval_only=True)] == ["T-1"]


async def test_eval_ticket_cannot_merge_or_retry_push(company: Company) -> None:
    h = make_harness(company, eval_only=True)
    await h.tickets.create(eval_ticket(), actor="eval")
    await h.runner.run_until_idle()
    await h.decide("T-1", "rejected", "eval xong")
    await h.runner.run_until_idle()
    assert (await h.tickets.get("T-1")).status is S.ESCALATED
    import pytest

    with pytest.raises(EscalationError, match="không bao giờ merge"):
        await merge_escalated(h.tickets, h.approvals, h.gateway, "T-1", actor="owner")
    with pytest.raises(EscalationError, match="không đẩy lại"):
        await retry_push(h.tickets, h.gateway, "T-1", actor="owner", code_role="coder")


SUITE = [
    {
        "id": "T-old",
        "project": PROJECT,
        "title": "Sửa nút",
        "body": "đề bài cũ",
        "labels": ["ui"],
        "base_sha": BASE_SHA,
        "observed": {"code_rounds": 2, "review_rounds": 2},
    },
    {"id": "T-skip", "skipped": "không đọc được MR"},
]


async def test_eval_start_is_idempotent_and_report_groups_runs(company: Company) -> None:
    import pytest

    from aico.rnd import evalrun

    h = make_harness(company, eval_only=True)
    created = await evalrun.start(h.tickets, SUITE, "thu-prompt", repeat=2)
    assert [new for _, new in created] == [True, True]
    again = await evalrun.start(h.tickets, SUITE, "thu-prompt", repeat=2)
    assert [new for _, new in again] == [False, False]
    t = await h.tickets.get(created[0][0])
    # Tiêu đề giữ nguyên, nhãn eval chỉ để lọc: prompt không cho agent biết mình bị chấm.
    assert t.labels == ["ui", "eval"] and t.title == SUITE[0]["title"]
    from aico.agents.llm_agent import ticket_block

    block = ticket_block(t)
    assert "eval" not in block.lower() and "nhãn: ui" in block
    assert eval_spec(t) == ("thu-prompt", BASE_SHA)
    with pytest.raises(evalrun.EvalError):
        evalrun.eval_tickets(SUITE, "Tên Sai")

    # Chưa chạy: báo đang chạy, chưa có kết quả.
    pending = await evalrun.report(h.tickets, "thu-prompt")
    assert pending["running"] == 2 and pending["total"] == {"runs": 0}

    h.gitlab.project(PROJECT).files["main"]["app.py"] = "x\n"
    await h.runner.run_until_idle()
    observed = {"T-old": SUITE[0]["observed"]}
    result = await evalrun.report(h.tickets, "thu-prompt", observed=observed, close=True)
    assert result["running"] == 0 and result["total"]["runs"] == 2
    line = result["tickets"][0]
    assert line["origin"] == "T-old" and line["runs"] == 2 and line["pass_rate"] == 1.0
    assert line["observed"] == {"code_rounds": 2, "review_rounds": 2}
    # --close: đóng lượt (từ chối merge), ticket thành escalated, vẫn tính là đạt.
    await h.runner.run_until_idle()
    assert {(await h.tickets.get(tid)).status for tid, _ in created} == {S.ESCALATED}
    closed = await evalrun.report(h.tickets, "thu-prompt")
    assert closed["total"]["pass_rate"] == 1.0
    # Biến thể khác không lẫn.
    assert (await evalrun.report(h.tickets, "khac"))["total"] == {"runs": 0}


async def test_label_alone_is_not_eval_and_variant_filter(company: Company) -> None:
    # Nhãn eval mà không có artifact (người/issue gắn được): ticket thường, aico chính nhận, phải
    # duyệt kế hoạch như mọi ticket (không có luật YAML "eval = low").
    h = make_harness(company)
    plain = Ticket(id="T-2", title="Sửa nút", project=PROJECT, labels=["eval"])
    await h.tickets.create(plain, actor="test")
    await h.runner.run_until_idle()
    assert (await h.tickets.get("T-2")).status is S.AWAITING_PLAN_APPROVAL
    # Instance eval chỉ nhận ticket của biến thể nó đang mount.
    await h.tickets.create(eval_ticket("T-3"), actor="eval")
    other = make_harness(company, tickets=h.tickets, eval_only=True)
    other.runner._eval_variant = "bien-the-khac"  # noqa: SLF001 - test
    await other.runner.run_until_idle()
    assert (await h.tickets.get("T-3")).status is S.NEW
    right = make_harness(company, tickets=h.tickets, eval_only=True)
    right.runner._eval_variant = "thu-prompt"  # noqa: SLF001
    await right.runner.run_until_idle()
    assert (await h.tickets.get("T-3")).status is S.AWAITING_MERGE_APPROVAL


async def test_approving_an_eval_ticket_still_never_merges(company: Company) -> None:
    h = make_harness(company, eval_only=True)
    await h.tickets.create(eval_ticket(), actor="eval")
    await h.runner.run_until_idle()
    await h.decide("T-1", "approved")
    await h.runner.run_until_idle()
    t = await h.tickets.get("T-1")
    assert t.status is S.ESCALATED
    assert all(m["state"] != "merged" for m in h.gitlab.project(PROJECT).mrs)
    reasons = [e.data.get("reason", "") for e in await h.tickets.events("T-1")]
    assert any("không merge" in r for r in reasons)


async def test_api_strips_eval_label_and_refuses_approving_eval_merge(company: Company) -> None:
    import httpx

    from aico.api import ApiDeps, create_api_app
    from aico.governance import InMemoryApprovalStore

    h = make_harness(company, eval_only=True)
    await h.tickets.create(eval_ticket(), actor="eval")
    await h.runner.run_until_idle()
    app = create_api_app(ApiDeps(tickets=h.tickets, approvals=InMemoryApprovalStore()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        made = await c.post("/tickets", json={"title": "x", "labels": ["Eval", "ui"]})
        assert made.json()["labels"] == ["ui"]
        body = {"decision": "approved", "actor": "owner"}
        assert (await c.post("/tickets/T-1/decisions", json=body)).status_code == 422
        body = {"decision": "rejected", "actor": "owner"}
        assert (await c.post("/tickets/T-1/decisions", json=body)).status_code == 202


async def test_claim_filters_eval_on_both_stores(store) -> None:  # type: ignore[no-untyped-def]
    await store.create(eval_ticket("T-e"), actor="eval")
    await store.create(Ticket(id="T-n", title="x", labels=["eval"]), actor="api")
    normal = await store.claim(owner="a", lease_seconds=30)
    assert normal is not None and normal.id == "T-n"
    assert await store.claim(owner="b", lease_seconds=30) is None
    assert await store.claim(owner="c", lease_seconds=30, eval_only=True, eval_variant="x") is None
    got = await store.claim(owner="d", lease_seconds=30, eval_only=True, eval_variant="thu-prompt")
    assert got is not None and got.id == "T-e"


def test_eval_suite_rejects_short_sha() -> None:
    import pytest

    from aico.rnd import evalrun

    with pytest.raises(evalrun.EvalError, match="40 ký tự"):
        evalrun.eval_tickets([{**SUITE[0], "base_sha": "abc1234"}], "v")


async def test_cleanup_deletes_branch_pair_of_closed_eval_tickets_only(company: Company) -> None:
    from aico.rnd import evalrun

    h = make_harness(company, eval_only=True)
    h.gitlab.project(PROJECT).files["main"]["app.py"] = "x\n"
    await h.tickets.create(eval_ticket("T-1"), actor="eval")
    await h.runner.run_until_idle()
    # Ticket thường có nhánh ai/T-9: không bao giờ bị dọn.
    await h.tickets.create(Ticket(id="T-9", title="thường", project=PROJECT), actor="t")
    await h.gitlab.create_branch(PROJECT, "ai/T-9", "main")
    delete = evalrun.gateway_deleter(h.gateway)
    files = h.gitlab.project(PROJECT).files

    # Còn chờ chấm: chưa dọn.
    rows = await evalrun.cleanup(h.tickets, "thu-prompt", delete)
    assert rows == [{"ticket": "T-1", "skipped": "chưa đóng (awaiting_merge_approval)"}]
    await evalrun.report(h.tickets, "thu-prompt", close=True)
    # Đã ghi quyết định đóng nhưng runner chưa xử lý: vẫn chưa dọn.
    assert "skipped" in (await evalrun.cleanup(h.tickets, "thu-prompt", delete))[0]
    await h.runner.run_until_idle()
    assert (await h.tickets.get("T-1")).status is S.ESCALATED

    # Đã tới chờ duyệt merge mà chưa chấm acceptance: giữ nhánh (xoá là mất thứ để chấm), trừ
    # khi bộ chuẩn không có acceptance cho mục đó, hoặc --force.
    ungraded = await evalrun.cleanup(h.tickets, "thu-prompt", delete)
    assert "chưa chấm acceptance" in ungraded[0]["skipped"]
    wanted = frozenset({"T-1"})  # ticket tạo tay: id gốc = id ticket
    assert (
        "skipped" in (await evalrun.cleanup(h.tickets, "thu-prompt", delete, need_grade=wanted))[0]
    )
    assert {"ai/T-1", "base/T-1"} <= set(files)
    forced = await evalrun.cleanup(h.tickets, "thu-prompt", delete, dry_run=True, force=True)
    assert forced == [{"ticket": "T-1", "would_delete": ["ai/T-1", "base/T-1"]}]
    none = frozenset[str]()  # bộ chuẩn không mục nào có acceptance
    dry = await evalrun.cleanup(h.tickets, "thu-prompt", delete, dry_run=True, need_grade=none)
    assert dry == [{"ticket": "T-1", "would_delete": ["ai/T-1", "base/T-1"]}]
    assert {"ai/T-1", "base/T-1"} <= set(files)
    assert await evalrun.cleanup(h.tickets, "khac", delete) == []  # biến thể khác

    rows = await evalrun.cleanup(h.tickets, "thu-prompt", delete, need_grade=none)
    assert rows == [{"ticket": "T-1", "deleted": ["ai/T-1", "base/T-1"]}]
    assert set(files) == {"main", "ai/T-9"}
    event = [e for e in await h.tickets.events("T-1") if e.kind == "eval_cleanup"][-1]
    assert "ai/T-1" in event.data["heads"]  # SHA trước khi xoá, để dựng lại khi cần
    entry = h.audit.entries[-1]
    assert (entry.role, entry.tool, entry.decision) == (
        "housekeeper",
        "delete_branch_pair",
        "allow",
    )
    assert entry.ticket_id == "T-1"
    again = await evalrun.cleanup(h.tickets, "thu-prompt", delete)
    assert again == [{"ticket": "T-1", "skipped": "đã dọn"}]


async def test_cleanup_keeps_open_escalations_and_stale_cleanup_event_expires(
    company: Company,
) -> None:
    from aico.rnd import evalrun

    h = make_harness(company, eval_only=True)
    h.gitlab.project(PROJECT).files["main"]["app.py"] = "x\n"
    delete = evalrun.gateway_deleter(h.gateway)
    files = h.gitlab.project(PROJECT).files
    none = frozenset[str]()

    async def clean(**kw: object) -> dict[str, object]:
        rows = await evalrun.cleanup(h.tickets, "thu-prompt", delete, need_grade=none, **kw)  # type: ignore[arg-type]
        return rows[0]

    await h.tickets.create(eval_ticket("T-1"), actor="eval")
    await h.runner.run_until_idle()
    # Người (không phải `report --close`) từ chối: ticket escalated nhưng còn xử lý tiếp được
    # (trả lại, chạy tiếp cần nhánh) -> không dọn, trừ khi owner xác nhận bỏ lượt (--escalated).
    await h.decide("T-1", "rejected", "xem lại")
    await h.runner.run_until_idle()
    assert (await h.tickets.get("T-1")).status is S.ESCALATED
    assert "escalated chưa đóng" in str((await clean())["skipped"])
    assert {"ai/T-1", "base/T-1"} <= set(files)
    assert (await clean(dry_run=True, include_escalated=True))["would_delete"]
    assert (await clean(include_escalated=True))["deleted"] == ["ai/T-1", "base/T-1"]
    assert (await clean(include_escalated=True))["skipped"] == "đã dọn"

    # Ticket chạy tiếp sau khi dọn (nhánh được tạo lại): event eval_cleanup cũ hết hiệu lực,
    # không khoá ticket ở "đã dọn" mãi.
    await h.tickets.transition("T-1", S.IN_PROGRESS, actor="owner")
    await h.gitlab.create_branch(PROJECT, "base/T-1", BASE_SHA)
    await h.gitlab.create_branch(PROJECT, "ai/T-1", BASE_SHA)
    assert (await clean())["skipped"] == "chưa đóng (in_progress)"
    await h.tickets.transition("T-1", S.ESCALATED, actor="orchestrator")
    # Quyết định đóng của eval ở vòng trước không còn tính cho lần escalated mới.
    assert "escalated chưa đóng" in str((await clean())["skipped"])
    assert (await clean(include_escalated=True))["deleted"] == ["ai/T-1", "base/T-1"]
    # failed/done là kết thúc hẳn: dọn không cần cờ.
    await h.tickets.create(eval_ticket("T-2"), actor="eval")
    await h.tickets.transition("T-2", S.FAILED, actor="t")
    rows = await evalrun.cleanup(h.tickets, "thu-prompt", delete, dry_run=True)
    assert rows[1] == {"ticket": "T-2", "would_delete": ["ai/T-2", "base/T-2"]}


async def test_cleanup_tool_is_guarded_and_hidden_from_agents(company: Company) -> None:
    import pytest

    from aico.rnd import evalrun
    from aico.tools import ToolDenied, ToolError

    h = make_harness(company)
    await h.gitlab.create_branch(PROJECT, "ai/T-9", "main")
    args = {"project": PROJECT, "branch": "ai/T-9"}
    # Chỉ vai trò hệ thống housekeeper được cấp; agent không thấy tool này.
    for role_id in company.roles:
        if role_id == evalrun.CLEANUP_ROLE:
            continue
        with pytest.raises(ToolDenied):
            await h.gateway.call(role_id, "gitlab", "delete_branch_pair", args, ticket_id=None)
        if company.role(role_id).grant_for("gitlab") is not None:
            assert "delete_branch_pair" not in {t.name for t in await h.gateway.tools_for(role_id)}
    assert company.role(evalrun.CLEANUP_ROLE).worker == "system"
    # Guard chặn tên nhánh ngoài ai/*, base/* và repo ngoài danh sách trước khi tới server.
    for bad in ({**args, "branch": "main"}, {**args, "project": "x/y"}):
        with pytest.raises(ToolDenied):
            await h.gateway.call("housekeeper", "gitlab", "delete_branch_pair", bad, ticket_id=None)
    # base/* qua guard (housekeeper tạo nhánh gốc) nhưng server chỉ nhận tên ai/<id>.
    with pytest.raises(ToolError, match="chỉ xoá được cặp nhánh"):
        await h.gateway.call(
            "housekeeper",
            "gitlab",
            "delete_branch_pair",
            {**args, "branch": "base/T-9"},
            ticket_id=None,
        )
    # Qua guard nhưng server từ chối: ai/T-9 là nhánh của ticket thường (không có base/T-9).
    with pytest.raises(ToolError, match="ticket thường"):
        await h.gateway.call("housekeeper", "gitlab", "delete_branch_pair", args, ticket_id=None)
    assert "ai/T-9" in h.gitlab.project(PROJECT).files
    # Lỗi của một ticket được báo trong dòng kết quả, không ghi event đã dọn.
    await h.tickets.create(eval_ticket("T-9"), actor="eval")
    await h.tickets.transition("T-9", S.FAILED, actor="t")
    rows = await evalrun.cleanup(h.tickets, "thu-prompt", evalrun.gateway_deleter(h.gateway))
    assert "ticket thường" in rows[0]["error"]
    assert not [e for e in await h.tickets.events("T-9") if e.kind == "eval_cleanup"]


async def test_only_system_role_creates_base_branch_from_full_sha(company: Company) -> None:
    import pytest

    from aico.tools import ToolDenied, ToolError

    h = make_harness(company, eval_only=True)
    files = h.gitlab.project(PROJECT).files

    async def create(role: str, branch: str, ref: str) -> dict[str, object]:
        args = {"project": PROJECT, "branch": branch, "ref": ref}
        return await h.gateway.call(role, "gitlab", "create_branch", args, ticket_id=None)

    # Agent (mọi vai trò trừ vai trò hệ thống) không tạo được base/*: không biến nhánh của ticket
    # thường thành cặp chạy lại (bị chặn vào main, xoá được bằng delete_branch_pair).
    for role_id in company.roles:
        if role_id != evalrun_role():
            with pytest.raises(ToolDenied):
                await create(role_id, "base/T-7", BASE_SHA)
    assert "base/T-7" not in files
    # Vai trò hệ thống: chỉ từ SHA đủ 40 ký tự, chỉ khi ai/<id> chưa có, tên một cấp.
    for ref in ("main", BASE_SHA[:12]):
        with pytest.raises(ToolError, match="SHA đủ 40 ký tự"):
            await create("housekeeper", "base/T-7", ref)
    with pytest.raises(ToolError, match="không hợp lệ"):
        await create("housekeeper", "base/a/b", BASE_SHA)
    await h.gitlab.create_branch(PROJECT, "ai/T-8", "main")  # ticket thường đang làm
    with pytest.raises(ToolError, match="đã tồn tại"):
        await create("housekeeper", "base/T-8", BASE_SHA)
    assert "base/T-8" not in files
    assert (await create("housekeeper", "base/T-7", BASE_SHA))["created"] is True
    # Gọi lại (publish vòng sau, khi ai/T-7 đã có): idempotent, không lỗi.
    await create("coder", "ai/T-7", BASE_SHA)
    assert (await create("housekeeper", "base/T-7", BASE_SHA))["created"] is False

    # Luồng thật: base/<id> do housekeeper tạo, ai/<id> do coder tạo.
    await h.tickets.create(eval_ticket("T-1"), actor="eval")
    await h.runner.run_until_idle()
    made = [
        (e.role, e.args.get("branch"))
        for e in h.audit.entries
        if e.tool == "create_branch" and e.ticket_id == "T-1" and e.decision == "allow"
    ]
    assert made[:2] == [("housekeeper", "base/T-1"), ("coder", "ai/T-1")]


def evalrun_role() -> str:
    from aico.orchestrator.workplan import EVAL_SYSTEM_ROLE

    return EVAL_SYSTEM_ROLE
