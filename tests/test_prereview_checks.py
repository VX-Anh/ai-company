"""Cổng trước review, kiểm tra `checks`: build/test/typecheck của repo chạy trên máy build."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from aico.agents.remote import STEP_MARK, ExecSettings, RemoteExec
from aico.core import Artifact, Ticket, TicketStatus
from aico.core.specs import PrereviewGateSpec, ProjectChecksSpec
from aico.orchestrator.ports import StepRunner
from aico.orchestrator.prereview import CHECKS, evaluate_checks, log_tail
from aico.registry import Company
from aico.rnd.metrics import aggregate, classify_escalation, ticket_metrics
from aico.tools import ToolGateway
from harness import BASE_SHA, PROJECT, Harness
from test_prereview_gate import OWNS_A, _Coder, _edit, _gates, _harness, _ok, _plan, _run

S = TicketStatus
STEPS = ProjectChecksSpec.model_validate(
    {
        "steps": [
            {"name": "typecheck", "run": "npm run typecheck"},
            {"name": "build", "run": "npm run build"},
            {"name": "test", "run": "npm test"},
        ]
    }
)
TSC_ERROR = "src/a.ts(3,7): error TS2322: Type 'string' is not assignable to type 'number'."


def _cfg(mode: str = "block", **kw: Any) -> PrereviewGateSpec:
    return PrereviewGateSpec.model_validate(
        {"checks": mode, "check_projects": {PROJECT: STEPS}, **kw}
    )


@dataclass
class _BuildMachine:
    """Máy build giả sau RemoteExec thật: mỗi lần POST /exec dùng kịch bản kế tiếp (cái cuối lặp
    lại). Kịch bản là {lệnh: mã thoát} (thiếu = 0), hoặc chuỗi: `infra` (máy build tự báo lỗi hạ
    tầng), `busy` (hết slot, HTTP 503), `down` (không kết nối được), `timeout` (hết giờ)."""

    script: list[dict[str, int] | str] = field(default_factory=lambda: [{}])
    logs: dict[str, str] = field(default_factory=dict)
    jobs: list[dict[str, Any]] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/exec"
        job = json.loads(request.content)
        plan = self.script[min(len(self.jobs), len(self.script) - 1)]
        self.jobs.append(job)
        if plan == "down":
            raise httpx.ConnectError("không kết nối được")
        if plan == "busy":
            return httpx.Response(
                503, json={"error": "máy build đang bận, thử lại sau", "infra_error": True}
            )
        if plan == "infra":
            return httpx.Response(
                200, json={"exit_code": 125, "output": "docker: lỗi volume", "infra_error": True}
            )
        if plan == "timeout":
            return httpx.Response(
                200,
                json={
                    "exit_code": 124,
                    "output": "hết thời gian, đã dừng container",
                    "infra_error": False,
                },
            )
        assert isinstance(plan, dict)
        lines = str(job["command"]).split("\n")
        out: list[str] = ["cài dependency...\n"]
        code = 0
        for i, line in enumerate(lines):
            if STEP_MARK in line and " start " in line:
                mark = line.split("'")[1].replace("\\n", "").replace(" start %s", "")
                command = lines[i + 2]  # dòng sau "(set -e" là lệnh của bước
                code = plan.get(command, 0)
                log = self.logs.get(command, f"log {command}")
                out.append(f"\n{mark} start 100\n{log}\n\n{mark} exit {code} 107\n")
                if code:
                    break
        return httpx.Response(
            200, json={"exit_code": code, "output": "".join(out), "infra_error": False}
        )

    def runner(self, gateway: ToolGateway) -> StepRunner:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return RemoteExec(
            ExecSettings("http://remote:8500", "worker-token", client), gateway, "coder"
        )


def _one_edit() -> _Coder:
    return _Coder([lambda items, fb: [_ok(items[0], _edit("src/a.ts"))]])


def _two_edits() -> _Coder:
    return _Coder(
        [
            lambda items, fb: [_ok(items[0], _edit("src/a.ts"))],
            lambda items, fb: [_ok(items[0], _edit("src/a.ts", "sửa kiểu\n"))],
        ]
    )


def _h(company: Company, coder: _Coder, machine: _BuildMachine, **kw: Any) -> Harness:
    kw.setdefault("prereview_gate", _cfg())
    return _harness(company, coder, _plan(OWNS_A), step_runner=machine.runner, **kw)


async def _head(h: Harness) -> str:
    return str((await h.gitlab.recent_commits(PROJECT, "ai/T-1"))[0]["id"])


async def test_green_checks_go_to_review_with_one_run_on_branch_head(company: Company) -> None:
    coder, machine = _one_edit(), _BuildMachine()
    h = _h(company, coder, machine)
    assert (await _run(h)).status is S.AWAITING_MERGE_APPROVAL
    assert len(coder.calls) == 1 and len(h.reviewer.requirements_seen) == 1
    # Một lần POST /exec gộp cả ba bước; giới hạn thời gian chung của cổng.
    (job,) = machine.jobs
    assert job["timeout_seconds"] == 900 and "image" not in job
    assert [c in job["command"] for c in ("npm run typecheck", "npm run build", "npm test")] == [
        True
    ] * 3
    (gate,) = await _gates(h)
    assert gate["passed"] is True and gate["checks_skipped"] is False
    assert gate["violations"] == [] and gate["would_block"] == []
    checks = gate["checks"]
    assert checks["passed"] is True and checks["mode"] == "block"
    assert checks["sha"] == await _head(h)  # đúng commit đầu nhánh ai/<id>
    assert checks["steps"] == [
        {"name": n, "exit_code": 0, "duration_seconds": 7} for n in ("typecheck", "build", "test")
    ]
    row = ticket_metrics(await h.tickets.get("T-1"), await h.tickets.events("T-1"))
    assert (row["gate_ci_runs"], row["gate_ci_failed"], row["gate_ci_skipped"]) == (1, 0, 0)
    ci = aggregate([row])["gate"]["ci"]
    assert ci["runs"] == 1 and ci["failed"] == 0 and ci["seconds_avg"] == ci["seconds_total"]


async def test_red_checks_in_default_warn_mode_only_record(company: Company) -> None:
    coder = _one_edit()
    machine = _BuildMachine([{"npm run typecheck": 2}], {"npm run typecheck": TSC_ERROR})
    # Cấu hình thật của công ty: checks ở mức warn, lệnh của aico-office lấy từ company/.
    h = _h(company, coder, machine, prereview_gate=None)
    assert company.sop("fix_ticket").prereview_gate.checks == "warn"
    assert (await _run(h)).status is S.AWAITING_MERGE_APPROVAL
    # Vẫn tới review ngay; coder không nhận góp ý nào của cổng.
    assert len(coder.calls) == 1 and coder.calls[0][1] is None
    assert len(machine.jobs) == 1 and len(h.reviewer.requirements_seen) == 1
    (gate,) = await _gates(h)
    assert gate["passed"] is True and gate["would_block"] == [CHECKS]
    assert gate["violations"] == [
        {"check": CHECKS, "count": 1, "paths": ["typecheck"], "mode": "warn"}
    ]
    checks = gate["checks"]
    assert checks["passed"] is False and checks["failed_step"] == "typecheck"
    # Bước sau bước hỏng không chạy; bước hỏng kèm mã thoát, thời gian và đuôi log.
    assert checks["steps"] == [
        {"name": "typecheck", "exit_code": 2, "duration_seconds": 7, "tail": TSC_ERROR}
    ]
    assert "feedback" not in gate
    row = ticket_metrics(await h.tickets.get("T-1"), await h.tickets.events("T-1"))
    assert row["gate_would_block"] == {CHECKS: 1} and row["gate_blocks"] == 0
    assert row["gate_ci_failed"] == 1
    agg = aggregate([row])["gate"]
    assert agg["would_block"] == {CHECKS: 1} and agg["ci"]["failed"] == 1


async def test_red_checks_in_block_mode_return_to_code_with_step_and_log_tail(
    company: Company,
) -> None:
    coder = _two_edits()
    noise = "\n".join(f"dòng log thừa {n} \x1b[31mđỏ\x1b[0m" for n in range(400))
    machine = _BuildMachine(
        [{"npm test": 1}, {}], {"npm test": f"ĐẦU LOG\n{noise}\nFAIL tests/a.test.ts > cộng"}
    )
    h = _h(company, coder, machine)
    assert (await _run(h)).status is S.AWAITING_MERGE_APPROVAL
    # Lần đầu đỏ: không tới reviewer, quay lại code đúng một lần; lần hai xanh mới review.
    assert len(coder.calls) == 2 and len(h.reviewer.requirements_seen) == 1
    assert len(machine.jobs) == 2
    feedback = coder.calls[1][1] or ""
    assert "[cổng trước review]" in feedback
    assert "Bước kiểm `test`" in feedback and "`npm test`" in feedback
    assert "mã thoát 1" in feedback and "FAIL tests/a.test.ts > cộng" in feedback
    # Đuôi log cắt gọn (đầu log và mã màu bị bỏ), không gửi nguyên log.
    assert "ĐẦU LOG" not in feedback and "\x1b" not in feedback and len(feedback) < 5000
    first, second = await _gates(h)
    assert first["passed"] is False and first["round"] == 0
    assert first["violations"] == [
        {"check": CHECKS, "count": 1, "paths": ["test"], "mode": "block"}
    ]
    assert first["feedback"] == feedback and first["would_block"] == []
    assert [s["name"] for s in first["checks"]["steps"]] == ["typecheck", "build", "test"]
    assert len(first["checks"]["steps"][2]["tail"]) <= 1501
    assert second["passed"] is True and second["checks"]["passed"] is True
    assert second["checks"]["sha"] != first["checks"]["sha"]  # mỗi lượt chạy trên đầu nhánh mới
    row = ticket_metrics(await h.tickets.get("T-1"), await h.tickets.events("T-1"))
    assert row["gate_blocks"] == 1 and row["gate_violations"] == {CHECKS: 1}
    assert (row["gate_ci_runs"], row["gate_ci_failed"]) == (2, 1)


async def test_checks_still_red_after_max_rounds_escalate(company: Company) -> None:
    coder = _two_edits()
    machine = _BuildMachine([{"npm run build": 1}])
    h = _h(company, coder, machine, prereview_gate=_cfg(max_rounds=1))
    t = await _run(h)
    assert t.status is S.ESCALATED and len(h.reviewer.requirements_seen) == 0
    assert len(coder.calls) == 2 and len(machine.jobs) == 2  # dùng chung giới hạn gate_rounds
    events = await h.tickets.events("T-1")
    reason = next(e for e in events if e.to_status is S.ESCALATED).data["reason"]
    assert reason.startswith("cổng trước review:") and "bước kiểm hỏng: build" in reason
    assert classify_escalation(reason) == "cổng trước review"


@pytest.mark.parametrize(
    ("failure", "note"),
    [
        ("infra", "lỗi hạ tầng"),
        ("busy", "đang bận"),
        ("down", "không phản hồi"),
        ("timeout", "quá giờ"),
    ],
)
async def test_build_machine_trouble_never_blocks(
    company: Company, failure: str, note: str
) -> None:
    coder, machine = _one_edit(), _BuildMachine([failure])
    h = _h(company, coder, machine)  # kể cả ở mức block
    assert (await _run(h)).status is S.AWAITING_MERGE_APPROVAL
    assert len(coder.calls) == 1 and len(h.reviewer.requirements_seen) == 1
    (gate,) = await _gates(h)
    assert gate["passed"] is True and gate["checks_skipped"] is True
    assert gate["violations"] == [] and gate["would_block"] == []
    assert any(note in n and "bỏ kiểm tra checks" in n for n in gate["notes"])
    row = ticket_metrics(await h.tickets.get("T-1"), await h.tickets.events("T-1"))
    assert row["gate_ci_skipped"] == 1 and row["gate_ci_failed"] == 0
    assert aggregate([row])["gate"]["ci"]["skipped"] == 1


@pytest.mark.parametrize(
    "gate",
    [
        _cfg(check_projects={}),  # repo không có cấu hình lệnh
        _cfg(check_projects={"VX-Anh/khac": STEPS}),
        _cfg("off"),
    ],
)
async def test_unconfigured_repo_or_off_skips_checks(
    company: Company, gate: PrereviewGateSpec
) -> None:
    coder, machine = _one_edit(), _BuildMachine([{"npm test": 1}])
    h = _h(company, coder, machine, prereview_gate=gate)
    assert (await _run(h)).status is S.AWAITING_MERGE_APPROVAL
    assert machine.jobs == []
    (event,) = await _gates(h)
    assert event["passed"] is True and "checks" not in event
    assert event["checks_skipped"] is False and event["notes"] == []


async def test_without_build_machine_checks_do_not_run(company: Company) -> None:
    coder = _one_edit()
    h = _harness(company, coder, _plan(OWNS_A), prereview_gate=_cfg())
    assert (await _run(h)).status is S.AWAITING_MERGE_APPROVAL
    (event,) = await _gates(h)
    assert "checks" not in event and event["checks_skipped"] is False


@pytest.mark.parametrize(
    ("labels", "kw"),
    [
        (["Tai-Lieu", "small-fix"], {"prereview_gate": _cfg(checks_skip_labels=["tai-lieu"])}),
        (["tri-thuc", "small-fix"], {"label_scopes": {"tri-thuc": ("src/",)}}),  # nhãn có phạm vi
        (["khong-test", "small-fix"], {"test_skip_labels": ("khong-test",)}),  # SOP bỏ bước test
    ],
)
async def test_non_code_tickets_skip_checks(
    company: Company, labels: list[str], kw: dict[str, Any]
) -> None:
    coder, machine = _one_edit(), _BuildMachine([{"npm test": 1}])
    h = _h(company, coder, machine, **kw)
    assert (await _run(h, labels=labels)).status is S.AWAITING_MERGE_APPROVAL
    assert machine.jobs == [] and len(coder.calls) == 1
    assert all("checks" not in g for g in await _gates(h))


async def test_replayed_ticket_runs_checks_without_revealing_it(company: Company) -> None:
    coder = _two_edits()
    machine = _BuildMachine([{"npm run typecheck": 2}, {}], {"npm run typecheck": TSC_ERROR})
    h = _h(company, coder, machine, eval_only=True)
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
    assert len(machine.jobs) == 2 and len(coder.calls) == 2
    first, second = await _gates(h)
    assert first["checks"]["failed_step"] == "typecheck" and second["checks"]["passed"] is True
    feedback = (coder.calls[1][1] or "").lower()
    assert "bước kiểm `typecheck`" in feedback and "ts2322" in feedback
    assert "eval" not in feedback and "chạy lại" not in feedback and "base/" not in feedback
    assert "eval" not in str(machine.jobs[0]["command"])


async def test_project_image_and_timeout_reach_build_machine(company: Company) -> None:
    spec = STEPS.model_copy(update={"image": "node", "timeout": 300})
    coder, machine = _one_edit(), _BuildMachine()
    h = _h(company, coder, machine, prereview_gate=_cfg(check_projects={PROJECT: spec}))
    assert (await _run(h)).status is S.AWAITING_MERGE_APPROVAL
    assert machine.jobs[0]["image"] == "node" and machine.jobs[0]["timeout_seconds"] == 300


def test_evaluate_checks_and_log_tail() -> None:
    names = ("typecheck", "build", "test")
    ok = {"exit_code": 0, "output": "ổn", "duration_seconds": 3}
    assert evaluate_checks(names, dict.fromkeys(names, ok)).passed is True
    red = evaluate_checks(names, {"typecheck": ok, "build": {"exit_code": 1, "output": "hỏng"}})
    assert (red.passed, red.failed_step, red.exit_code) == (False, "build", 1)
    assert [s["name"] for s in red.steps] == ["typecheck", "build"]
    assert red.violation("npm run build").details == ("npm run build", "1", "hỏng")
    # Thiếu bước mà không bước nào hỏng (log mất dòng đánh dấu): không kết luận, không chặn.
    assert evaluate_checks(names, {}).passed is None
    assert evaluate_checks(names, {"typecheck": ok}).passed is None
    # Mã 124 do chính lệnh trả (có thời gian của bước) là kết quả của bài làm, không phải hết giờ.
    own = {"exit_code": 124, "output": "hết thời gian", "duration_seconds": 5}
    assert evaluate_checks(names, {"typecheck": own}).passed is False
    assert log_tail("a\r\n\x1b[1mb\x1b[0m\nc\n", 100, 2) == "b\nc"
    assert log_tail("x" * 50, 10) == "…" + "x" * 10


def test_company_check_projects_and_spec_validation(company: Company) -> None:
    cfg = company.sop("fix_ticket").prereview_gate
    assert cfg.checks == "warn" and cfg.checks_skip_labels == ("research", "docs", "retro")
    commands = {
        project: [s.run for s in spec.steps] for project, spec in cfg.check_projects.items()
    }
    assert commands == {
        "VX-Anh/aico-office": ["npm run typecheck", "npm run build", "npm test"],
    }
    # Pha chạy lệnh của máy build không có mạng: lệnh cài dependency không được nằm ở đây.
    assert not any(" ci" in c or "install" in c for runs in commands.values() for c in runs)
    assert PrereviewGateSpec().checks == "warn" and PrereviewGateSpec().check_projects == {}
    assert PrereviewGateSpec.model_validate({"checks": False}).checks == "off"
    with pytest.raises(ValidationError):
        PrereviewGateSpec.model_validate({"checks": True})
    for bad in (
        {"steps": []},
        {"steps": [{"name": "Có dấu cách", "run": "x"}]},
        {"steps": [{"name": "a", "run": "x"}, {"name": "a", "run": "y"}]},
        {"steps": [{"name": "a", "run": "x"}], "timeout": 0},
    ):
        with pytest.raises(ValidationError):
            ProjectChecksSpec.model_validate(bad)
