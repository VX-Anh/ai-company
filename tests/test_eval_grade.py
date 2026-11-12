"""Chấm acceptance của ticket chạy lại: lệnh build/test của bộ chuẩn chạy trên máy build với mã
nguồn nhánh ai/<id>; báo cáo tính tỷ lệ đạt và chỉ số chặn "review đạt mà acceptance hỏng"."""

from __future__ import annotations

import base64
import io
import json
import tarfile
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from aico.agents.remote import ExecSettings, RemoteExec, RemoteExecError
from aico.core import TicketStatus
from aico.registry import Company
from aico.rnd import evalrun
from harness import PROJECT, make_harness

S = TicketStatus
VARIANT = "thu-prompt"
SUITE: list[dict[str, Any]] = [
    {
        "id": "T-old",
        "project": PROJECT,
        "title": "Sửa nút",
        "base_sha": "main",
        "acceptance": {"build": "npm run build", "test": "npm test"},
    },
    {"id": "T-plain", "project": PROJECT, "title": "Không có lệnh chấm", "base_sha": "main"},
]


def _fake_runner(
    exit_codes: dict[str, dict[str, int]], calls: list[tuple[str, str, str, tuple[str, ...]]]
) -> evalrun.AcceptanceRunner:
    """Máy build giả: exit_codes[ticket][bước]; dừng ở bước đầu tiên hỏng như RemoteExec."""

    async def run(
        ticket_id: str, project: str, ref: str, commands: Mapping[str, str], image: str | None
    ) -> dict[str, dict[str, Any]]:
        calls.append((ticket_id, project, ref, tuple(commands)))
        steps: dict[str, dict[str, Any]] = {}
        for name in commands:
            code = exit_codes.get(ticket_id, {}).get(name, 0)
            steps[name] = {"exit_code": code, "output": "x" * 5000, "duration_seconds": 1.5}
            if code:
                break
        return steps

    return run


def test_acceptance_of_validates_suite_item() -> None:
    assert evalrun.acceptance_of({"id": "a"}) == {}
    got = evalrun.acceptance_of({"acceptance": {"test": "npm test", "build": " npm ci "}})
    assert list(got.items()) == [("build", "npm ci"), ("test", "npm test")]  # build trước test
    assert evalrun.acceptance_of({"acceptance": {"test": "t", "build": "", "image": "x"}}) == {
        "test": "t"
    }
    with pytest.raises(evalrun.EvalError, match="khoá lạ"):
        evalrun.acceptance_of({"id": "a", "acceptance": {"lint": "x"}})
    with pytest.raises(evalrun.EvalError):
        evalrun.acceptance_of({"id": "a", "acceptance": "npm test"})


async def test_grade_records_result_and_report_shows_lenient_review(company: Company) -> None:
    h = make_harness(company, eval_only=True)
    h.gitlab.project(PROJECT).files["main"]["app.py"] = "x\n"
    created = [tid for tid, _ in await evalrun.start(h.tickets, SUITE, VARIANT, repeat=2)]
    by_origin: dict[str, list[str]] = {}
    for tid in created:
        ticket = await h.tickets.get(tid)
        by_origin.setdefault((ticket.source_ref or "").split(":")[2], []).append(tid)
    good, bad = by_origin["T-old"]
    calls: list[tuple[str, str, str, tuple[str, ...]]] = []
    run = _fake_runner({bad: {"build": 0, "test": 1}}, calls)

    # Chưa tới chờ duyệt merge: không có gì để chấm.
    assert await evalrun.grade(h.tickets, VARIANT, SUITE, run) == []
    await h.runner.run_until_idle()
    before = await evalrun.report(h.tickets, VARIANT)
    assert "acceptance_pass_rate" not in before["total"]  # chưa chấm thì không bịa số

    rows = await evalrun.grade(h.tickets, VARIANT, SUITE, run)
    # Chỉ mục có acceptance được chấm, trên nhánh làm việc của chính ticket đó.
    assert {r["ticket"] for r in rows} == {good, bad}
    assert sorted(calls) == sorted(
        (tid, PROJECT, f"ai/{tid}", ("build", "test")) for tid in (good, bad)
    )
    result = {r["ticket"]: r for r in rows}
    assert result[good]["passed"] is True and result[bad]["passed"] is False
    assert result[bad]["steps"] == {"build": 0, "test": 1}
    event = [e for e in await h.tickets.events(bad) if e.kind == "eval_acceptance"][-1]
    assert event.data["passed"] is False and event.data["ref"] == f"ai/{bad}"
    assert len(event.data["steps"]["test"]["output_tail"]) == 2000  # không chép cả log vào event
    # Agent không thấy gì: trạng thái ticket không đổi, không có quyết định nào được ghi.
    assert (await h.tickets.get(bad)).status is S.AWAITING_MERGE_APPROVAL
    assert (await h.tickets.get(bad)).pending_resume is None

    # Gọi lại: không chấm lại (trừ khi --regrade).
    again = await evalrun.grade(h.tickets, VARIANT, SUITE, run)
    assert {r["skipped"] for r in again} == {"đã chấm"} and len(calls) == 2
    redo = await evalrun.grade(h.tickets, VARIANT, SUITE, _fake_runner({}, calls), regrade=True)
    assert all(r["passed"] for r in redo) and len(calls) == 4
    await evalrun.grade(h.tickets, VARIANT, SUITE, run, regrade=True)  # về lại: bad hỏng test

    report = await evalrun.report(h.tickets, VARIANT)
    total = report["total"]
    # 4 lượt đều qua review; 2 lượt được chấm, 1 hỏng acceptance => Reviewer cho đạt bài hỏng.
    assert total["pass_rate"] == 1.0 and total["acceptance_graded"] == 2
    assert total["acceptance_pass_rate"] == 0.5
    assert total["review_pass_acceptance_fail_rate"] == 0.5
    assert total["acceptance_ungraded"] == 2  # hai lượt của mục không có lệnh chấm
    line = {t["origin"]: t for t in report["tickets"]}
    assert line["T-old"]["acceptance_pass_rate"] == 0.5
    assert "acceptance_pass_rate" not in line["T-plain"]


async def test_infra_error_is_not_a_failed_acceptance_and_close_waits_for_grade(
    company: Company,
) -> None:
    h = make_harness(company, eval_only=True)
    h.gitlab.project(PROJECT).files["main"]["app.py"] = "x\n"
    created = [tid for tid, _ in await evalrun.start(h.tickets, SUITE[:1], VARIANT, repeat=1)]
    await h.runner.run_until_idle()
    tid = created[0]

    async def down(*_: Any) -> dict[str, dict[str, Any]]:
        raise RemoteExecError("máy build không phản hồi")

    rows = await evalrun.grade(h.tickets, VARIANT, SUITE, down)
    assert rows == [{"ticket": tid, "origin": "T-old", "error": "máy build không phản hồi"}]
    assert not [e for e in await h.tickets.events(tid) if e.kind == "eval_acceptance"]

    # --close kèm bộ chuẩn: mục có acceptance mà chưa chấm thì chưa đóng (đóng + dọn là mất nhánh).
    need = frozenset({"T-old"})
    rep = await evalrun.report(h.tickets, VARIANT, close=True, need_grade=need)
    assert rep["close_skipped_ungraded"] == 1
    assert (await h.tickets.get(tid)).pending_resume is None

    # Build hỏng: test không chạy, vẫn là hỏng acceptance.
    calls: list[tuple[str, str, str, tuple[str, ...]]] = []
    rows = await evalrun.grade(h.tickets, VARIANT, SUITE, _fake_runner({tid: {"build": 2}}, calls))
    assert rows[0]["passed"] is False and rows[0]["steps"] == {"build": 2}
    rep = await evalrun.report(h.tickets, VARIANT, close=True, need_grade=need)
    assert "close_skipped_ungraded" not in rep
    await h.runner.run_until_idle()
    assert (await h.tickets.get(tid)).status is S.ESCALATED
    # Đã đóng nhưng chưa dọn: vẫn chấm lại được; dọn rồi thì thôi.
    redo = await evalrun.grade(h.tickets, VARIANT, SUITE, _fake_runner({}, calls), regrade=True)
    assert redo[0]["passed"] is True
    await evalrun.cleanup(h.tickets, VARIANT, evalrun.gateway_deleter(h.gateway))
    gone = await evalrun.grade(h.tickets, VARIANT, SUITE, _fake_runner({}, calls), regrade=True)
    assert gone == [{"ticket": tid, "origin": "T-old", "skipped": "đã dọn nhánh"}]


async def test_remote_exec_sends_branch_archive_and_stops_at_first_failure(
    company: Company,
) -> None:
    h = make_harness(company)
    await h.gitlab.create_branch(PROJECT, "ai/T-1", "main")
    h.gitlab.project(PROJECT).files["ai/T-1"]["app.py"] = "print('mới')\n"
    jobs: list[dict[str, Any]] = []
    codes = {"npm run build": 0, "npm test": 3, "npm run e2e": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/exec"
        assert request.headers["authorization"] == "Bearer worker-token"
        job = json.loads(request.content)
        jobs.append(job)
        return httpx.Response(
            200, json={"exit_code": codes[job["command"]], "output": "log", "duration_seconds": 2}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        remote = RemoteExec(ExecSettings("http://remote:8500/", "worker-token", client), h.gateway)
        steps = await remote.run(
            "T-1",
            PROJECT,
            "ai/T-1",
            {"build": "npm run build", "test": "npm test", "e2e": "npm run e2e"},
            image="node",
        )
        assert {k: v["exit_code"] for k, v in steps.items()} == {"build": 0, "test": 3}
        assert [j["command"] for j in jobs] == ["npm run build", "npm test"]
        assert jobs[0]["strip"] == 1 and jobs[0]["image"] == "node"
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(jobs[0]["archive"]))) as tar:
            member = next(m for m in tar.getmembers() if m.name.endswith("/app.py"))
            file = tar.extractfile(member)
            assert file is not None and file.read() == b"print('m\xe1\xbb\x9bi')\n"
        # Mã nguồn tải qua gateway (guard + audit), bằng vai trò coder, gắn với ticket.
        entry = h.audit.entries[-1]
        assert (entry.role, entry.tool, entry.ticket_id) == ("coder", "download_archive", "T-1")

        with pytest.raises(RemoteExecError, match="không tải được"):
            await remote.run("T-1", PROJECT, "ai/khong-co", {"build": "npm run build"})

    def infra(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"exit_code": 125, "output": "docker lỗi"})

    def refused(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "sai token"})

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("máy build tắt")

    for bad, message in ((infra, "hạ tầng"), (refused, "sai token"), (offline, "không phản hồi")):
        async with httpx.AsyncClient(transport=httpx.MockTransport(bad)) as client:
            remote = RemoteExec(ExecSettings("http://remote:8500", "t", client), h.gateway)
            with pytest.raises(RemoteExecError, match=message):
                await remote.run("T-1", PROJECT, "ai/T-1", {"build": "npm run build"})
