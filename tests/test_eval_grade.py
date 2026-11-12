"""Chấm acceptance của ticket chạy lại: lệnh build/test của bộ chuẩn chạy trên máy build với mã
nguồn nhánh ai/<id>; báo cáo tính tỷ lệ đạt và chỉ số chặn "review đạt mà acceptance hỏng"."""

from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
import tarfile
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from aico.agents.remote import (
    STEP_MARK,
    ExecSettings,
    RemoteExec,
    RemoteExecError,
    split_steps,
    steps_script,
)
from aico.core import TicketStatus
from aico.registry import Company
from aico.rnd import evalrun
from harness import BASE_SHA, PROJECT, make_harness

S = TicketStatus
VARIANT = "thu-prompt"
HEAD_SHA = "d" * 40
SUITE: list[dict[str, Any]] = [
    {
        "id": "T-old",
        "project": PROJECT,
        "title": "Sửa nút",
        "base_sha": BASE_SHA,
        "acceptance": {"build": "npm run build", "test": "npm test"},
    },
    {"id": "T-plain", "project": PROJECT, "title": "Không có lệnh chấm", "base_sha": BASE_SHA},
]


def _fake_runner(
    exit_codes: dict[str, dict[str, int]], calls: list[tuple[str, str, str, tuple[str, ...]]]
) -> evalrun.AcceptanceRunner:
    """Máy build giả: exit_codes[ticket][bước]; dừng ở bước đầu tiên hỏng như RemoteExec."""

    async def run(
        ticket_id: str, project: str, ref: str, commands: Mapping[str, str], image: str | None
    ) -> tuple[str | None, dict[str, dict[str, Any]]]:
        calls.append((ticket_id, project, ref, tuple(commands)))
        steps: dict[str, dict[str, Any]] = {}
        for name in commands:
            code = exit_codes.get(ticket_id, {}).get(name, 0)
            steps[name] = {"exit_code": code, "output": "x" * 5000, "duration_seconds": 1.5}
            if code:
                break
        return HEAD_SHA, steps

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
    # Ghim commit: kết quả gắn với đúng SHA đã chấm, không chỉ tên nhánh.
    assert event.data["sha"] == HEAD_SHA and result[bad]["sha"] == HEAD_SHA
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

    async def down(*_: Any) -> tuple[str | None, dict[str, dict[str, Any]]]:
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
    await evalrun.cleanup(h.tickets, VARIANT, evalrun.gateway_deleter(h.gateway), need_grade=need)
    gone = await evalrun.grade(h.tickets, VARIANT, SUITE, _fake_runner({}, calls), regrade=True)
    assert gone == [{"ticket": tid, "origin": "T-old", "skipped": "đã dọn nhánh"}]


def _exec_response(job: dict[str, Any], codes: Mapping[str, int]) -> dict[str, Any]:
    """Máy build giả cho lệnh gộp của RemoteExec: "chạy" từng bước theo dòng đánh dấu, mã thoát
    lấy theo lệnh của bước (codes), dừng ở bước đầu tiên hỏng như sh thật."""
    lines = str(job["command"]).split("\n")
    out: list[str] = ["cài dependency...\n"]
    code = 0
    for i, line in enumerate(lines):
        if STEP_MARK in line and " start " in line:
            mark = line.split("'")[1].replace("\\n", "").replace(" start %s", "")
            code = codes[lines[i + 2]]  # dòng sau "(set -e" là lệnh của bước
            out.append(f"\n{mark} start 100\nlog {lines[i + 2]}\n\n{mark} exit {code} 103\n")
            if code:
                break
    return {"exit_code": code, "output": "".join(out), "duration_seconds": 3, "infra_error": False}


async def test_remote_exec_runs_all_steps_in_one_container_on_pinned_sha(
    company: Company,
) -> None:
    h = make_harness(company)
    await h.gitlab.create_branch(PROJECT, "ai/T-1", "main")
    head = await h.gitlab.commit(
        PROJECT,
        "ai/T-1",
        "sửa",
        [{"action": "update", "file_path": "app.py", "content": "print('mới')\n"}],
    )
    jobs: list[dict[str, Any]] = []
    codes = {"npm run build": 0, "npm test": 3, "npm run e2e": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/exec"
        assert request.headers["authorization"] == "Bearer worker-token"
        job = json.loads(request.content)
        jobs.append(job)
        return httpx.Response(200, json=_exec_response(job, codes))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        remote = RemoteExec(
            ExecSettings("http://remote:8500/", "worker-token", client),
            h.gateway,
            evalrun.CLEANUP_ROLE,
        )
        done = await remote.run(
            "T-1",
            PROJECT,
            "ai/T-1",
            {"build": "npm run build", "test": "npm test", "e2e": "npm run e2e"},
            image="node",
        )
        # Một lần POST /exec cho mọi bước: cùng volume (test thấy sản phẩm của build), một lần
        # cài dependency; kết quả từng bước tách theo dòng đánh dấu.
        assert len(jobs) == 1 and jobs[0]["strip"] == 1 and jobs[0]["image"] == "node"
        assert {k: v["exit_code"] for k, v in done.steps.items()} == {"build": 0, "test": 3}
        assert done.steps["test"]["output"] == "log npm test"
        assert done.steps["build"]["duration_seconds"] == 3
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(jobs[0]["archive"]))) as tar:
            member = next(m for m in tar.getmembers() if m.name.endswith("/app.py"))
            file = tar.extractfile(member)
            assert file is not None and file.read() == b"print('m\xe1\xbb\x9bi')\n"
        # Ghim commit: đọc SHA đầu nhánh rồi tải archive theo SHA đó, đều qua gateway (guard +
        # audit) bằng vai trò hệ thống (không mượn quyền của agent), gắn với ticket.
        assert done.sha == head["id"]
        read = [(e.role, e.tool, e.args.get("ref"), e.ticket_id) for e in h.audit.entries[-2:]]
        assert read == [
            ("housekeeper", "branch_head", "ai/T-1", "T-1"),
            ("housekeeper", "download_archive", head["id"], "T-1"),
        ]

        with pytest.raises(RemoteExecError, match="không tải được"):
            await remote.run("T-1", PROJECT, "ai/khong-co", {"build": "npm run build"})

    def reply(body: dict[str, Any], status: int = 200) -> Any:
        return lambda request: httpx.Response(status, json=body)

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("máy build tắt")

    for bad, message in (
        (reply({"exit_code": 125, "output": "docker lỗi", "infra_error": True}), "hạ tầng"),
        # Máy build bản cũ (chưa có infra_error): vẫn đoán theo mã thoát như trước.
        (reply({"exit_code": 255, "output": "ssh"}), "hạ tầng"),
        (reply({"error": "máy build đang bận", "infra_error": True}, 503), "đang bận"),
        (reply({"error": "sai token"}, 401), "sai token"),
        (offline, "không phản hồi"),
    ):
        async with httpx.AsyncClient(transport=httpx.MockTransport(bad)) as client:
            remote = RemoteExec(ExecSettings("http://remote:8500", "t", client), h.gateway, "coder")
            with pytest.raises(RemoteExecError, match=message):
                await remote.run("T-1", PROJECT, "ai/T-1", {"build": "npm run build"})

    # Lệnh của bài làm tự thoát 125/255: là kết quả (hỏng acceptance), không phải lỗi hạ tầng,
    # nên được ghi lại thay vì treo lượt chấm mãi.
    for code in (125, 255):

        def own(request: httpx.Request, code: int = code) -> httpx.Response:
            job = json.loads(request.content)
            return httpx.Response(200, json=_exec_response(job, {"npm test": code}))

        async with httpx.AsyncClient(transport=httpx.MockTransport(own)) as client:
            remote = RemoteExec(ExecSettings("http://remote:8500", "t", client), h.gateway, "coder")
            done = await remote.run("T-1", PROJECT, "ai/T-1", {"test": "npm test"})
            assert done.steps["test"]["exit_code"] == code


@pytest.mark.skipif(shutil.which("sh") is None, reason="cần sh")
def test_steps_script_shares_workdir_and_reports_each_step(tmp_path: Any) -> None:
    def run(commands: dict[str, str]) -> dict[str, dict[str, Any]]:
        script = steps_script(commands, "n0nce")
        done = subprocess.run(
            ["sh", "-c", script], cwd=tmp_path, capture_output=True, text=True, check=False
        )
        return split_steps(done.stdout + done.stderr, done.returncode, list(commands), "n0nce")

    # test thấy file build tạo ra (cùng thư mục làm việc); mỗi bước có log và mã thoát riêng.
    steps = run({"build": "echo dung > out.txt; echo xong-build", "test": "cat out.txt; exit 4"})
    assert (steps["build"]["exit_code"], steps["build"]["output"]) == (0, "xong-build")
    assert steps["build"]["duration_seconds"] in (0, 1)
    assert steps["test"]["exit_code"] == 4 and "dung" in steps["test"]["output"]
    # Bước hỏng giữa chừng (set -e trong bước): dừng, bước sau không chạy; lệnh `a && b` hỏng ở a
    # cũng là hỏng. Bài làm in dòng đánh dấu giả (không biết nonce) không đổi được kết quả.
    steps = run({"build": "false && true", "test": "echo không-chạy"})
    assert list(steps) == ["build"] and steps["build"]["exit_code"] == 1
    steps = run({"build": "echo '::aico-step x build exit 0 1'; ls /khong-co-thu-muc; echo sau"})
    assert steps["build"]["exit_code"] != 0 and "sau" not in steps["build"]["output"]


def test_split_steps_without_markers_or_with_cut_log() -> None:
    names = ["build", "test"]
    # Không có dòng đánh dấu (cài dependency hỏng, hết giờ): tính cho bước đầu.
    assert split_steps("npm ci lỗi", 1, names, "n") == {
        "build": {"exit_code": 1, "output": "npm ci lỗi", "duration_seconds": None}
    }
    assert split_steps("", 0, names, "n") == {}
    # Log bị cắt đầu (mất dòng của build) nhưng test đã chạy: build đã đạt.
    cut = "...\n::aico-step n test start 5\nFAIL\n::aico-step n test exit 1 9\n"
    assert split_steps(cut, 1, names, "n") == {
        "build": {"exit_code": 0, "output": "", "duration_seconds": None},
        "test": {"exit_code": 1, "output": "FAIL", "duration_seconds": 4},
    }
    # Container bị dừng giữa bước (không có dòng kết thúc): bước đó nhận mã thoát chung.
    killed = "::aico-step n build start 5\nđang dựng\n"
    assert split_steps(killed, 137, names, "n")["build"]["exit_code"] == 137
