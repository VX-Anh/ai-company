"""Kế hoạch có cấu trúc: đọc đầu việc, chạy song song theo phụ thuộc, tài liệu trong repo."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import tarfile
from typing import Any

import httpx

from aico.agents import CliAgentRunner, CliCoder, CliSettings
from aico.agents.cli_agent import overlay_archive
from aico.core import Ticket, TicketStatus
from aico.llm_relay import TaskTokens
from aico.orchestrator import Runner
from aico.orchestrator.ports import FileChange
from aico.orchestrator.workplan import owns_overlap, parse_work_plan
from aico.registry import Company
from aico.tools import ToolGateway
from harness import PROJECT, make_harness, text_files

WORKERS = {
    "claude": "http://claude:8400",
    "codex": "http://codex:8400",
    "cursor": "http://cursor:8400",
}
ROLES = ("coder", "designer")


def _plan(items: list[dict[str, Any]], mermaid: str | None = "graph TD\n  A --> B") -> str:
    text = "## Kế hoạch\nLàm theo các bước.\n\n```json\n" + json.dumps({"items": items}) + "\n```\n"
    if mermaid:
        text += f"\n```mermaid\n{mermaid}\n```\n"
    return text


def test_parse_work_plan_reads_items_and_architecture() -> None:
    text = _plan(
        [
            {"id": "W1", "title": "Layout", "owns": ["./src/scene/layout.ts"]},
            {"id": "W2", "title": "Sprite", "role": "designer", "owns": ["art/"]},
            {"id": "W3", "title": "Ghép", "owns": ["src/main.ts"], "depends_on": ["W1", "W2"]},
        ]
    )
    plan = parse_work_plan(text, ROLES)
    assert [(i.id, i.role, i.owns, i.depends_on) for i in plan.items] == [
        ("W1", "coder", ("src/scene/layout.ts",), ()),
        ("W2", "designer", ("art/",), ()),
        ("W3", "coder", ("src/main.ts",), ("W1", "W2")),
    ]
    assert plan.architecture == "graph TD\n  A --> B" and plan.notes == ()
    assert plan.items[1].allows("art/agent.pixel.json") and not plan.items[1].allows("src/a.ts")


def test_parse_work_plan_repairs_bad_plans() -> None:
    plan = parse_work_plan(
        _plan(
            [
                {"id": "W1", "owns": ["src/scene/"]},
                {
                    "id": "W1",
                    "role": "hacker",
                    "owns": ["src/scene/tiles.ts"],
                    "depends_on": ["W9"],
                },
                {"id": "W3"},
            ]
        ),
        ROLES,
    )
    w1, w2, w3 = plan.items
    assert w2.id == "W2" and w2.role == "coder"  # id trùng, vai trò lạ, phụ thuộc không tồn tại
    assert "W1" in w2.depends_on  # sửa chung src/scene/ với W1 nên phải chạy sau W1
    assert w3.owns == ("**",) and set(w3.depends_on) >= {"W2"}  # owns thiếu = mọi file
    assert len(plan.notes) >= 4

    cyclic = parse_work_plan(
        _plan(
            [
                {"id": "A", "owns": ["a.ts"], "depends_on": ["B"]},
                {"id": "B", "owns": ["b.ts"], "depends_on": ["A"]},
            ]
        ),
        ROLES,
    )
    assert [(i.id, i.depends_on) for i in cyclic.items] == [("A", ()), ("B", ("A",))]

    plain = parse_work_plan("Chỉ có văn bản, không có khối json.", ROLES)
    assert [(i.id, i.owns) for i in plain.items] == [("W1", ("**",))]
    assert plain.architecture is None


def test_owns_overlap() -> None:
    assert owns_overlap(("src/a.ts",), ("src/a.ts",))
    assert owns_overlap(("src/scene/",), ("src/scene/tiles.ts",))
    assert owns_overlap(("src/*.ts",), ("src/main.ts",))
    assert owns_overlap(("**",), ("anything",))
    assert not owns_overlap(("src/a.ts",), ("src/b.ts",))
    assert not owns_overlap(("src/scene/",), ("art/",))


def _tar(files: dict[str, str]) -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(f"repo-abc/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


def _untar(b64: str) -> dict[str, str]:
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(b64)), mode="r:gz") as tar:
        return {
            m.name.split("/", 1)[1]: tar.extractfile(m).read().decode()  # type: ignore[union-attr]
            for m in tar.getmembers()
            if m.isfile()
        }


def test_overlay_archive_applies_changes() -> None:
    base = _tar({"a.ts": "1", "b.ts": "2", "c.ts": "3"})
    out = overlay_archive(
        base,
        [
            FileChange("a.ts", "1+", "update"),
            FileChange("b.ts", action="delete"),
            FileChange("new/d.ts", "4", "create"),
        ],
    )
    assert _untar(out) == {"a.ts": "1+", "c.ts": "3", "new/d.ts": "4"}
    assert overlay_archive(base, []) == base


def test_overlay_archive_writes_binary_changes() -> None:
    png = bytes([0x89, 0x50, 0xFF])
    change = FileChange.from_arg(
        {
            "path": "img/a.png",
            "action": "create",
            "content": base64.b64encode(png).decode(),
            "encoding": "base64",
        }
    )
    assert change.to_arg()["encoding"] == "base64"
    out = overlay_archive(_tar({"a.ts": "1"}), [change])
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(out)), mode="r:gz") as tar:
        assert tar.extractfile("repo-abc/img/a.png").read() == png  # type: ignore[union-attr]


class _PlanText:
    def __init__(self, text: str) -> None:
        self.text = text

    async def plan(self, ticket: Ticket, feedback: str | None, requirements: str = "") -> str:
        return self.text


async def test_items_run_in_parallel_across_rooms_and_write_docs(company: Company) -> None:
    started = {"W1": asyncio.Event(), "W2": asyncio.Event()}
    jobs: dict[str, dict[str, Any]] = {}

    async def worker(request: httpx.Request) -> httpx.Response:
        job = json.loads(request.content)
        item = job["prompt"].split("đầu việc ", 1)[1].split(" ", 1)[0]
        jobs[item] = {
            "room": request.url.host,
            "role": job["role"],
            "files": _untar(job["workspace"]["data"]),
        }
        if item in started:
            started[item].set()
            # Bằng chứng chạy song song: W1 và W2 phải cùng đang chạy.
            await asyncio.wait_for(started["W2" if item == "W1" else "W1"].wait(), timeout=2)
        changes = {
            "W1": [{"path": "src/a.ts", "action": "update", "content": "a2\n"}],
            "W2": [
                {"path": "art/hero.pixel.json", "action": "create", "content": "{}\n"},
                {"path": "src/a.ts", "action": "update", "content": "ngoài phạm vi\n"},
            ],
            "W3": [{"path": "src/main.ts", "action": "update", "content": "main2\n"}],
        }[item]
        result = f"Xong {item}.\nTÓM TẮT: làm {item}"
        return httpx.Response(200, json={"ok": True, "result": result, "changes": changes})

    plan_text = _plan(
        [
            {"id": "W1", "title": "Logic", "owns": ["src/a.ts"]},
            {"id": "W2", "title": "Sprite", "role": "designer", "owns": ["art/"]},
            {"id": "W3", "title": "Ghép", "owns": ["src/main.ts"], "depends_on": ["W1", "W2"]},
        ],
        mermaid="graph TD\n  scene --> main",
    )

    def coder_factory(gateway: ToolGateway) -> CliCoder:
        client = httpx.AsyncClient(transport=httpx.MockTransport(worker))
        runner = CliAgentRunner(
            company, TaskTokens(), CliSettings(WORKERS, "worker-secret", "u", 0.01, client)
        )
        return CliCoder(runner, gateway, default_project=PROJECT)

    h = make_harness(company, planner=lambda gw: _PlanText(plan_text), coder=coder_factory)
    repo = h.gitlab.project(PROJECT)
    repo.files["main"].update({"src/a.ts": "a1\n", "src/main.ts": "main1\n"})
    await h.new_ticket()
    await h.runner.run_until_idle()
    events = await h.tickets.events("T-1")
    approval = next(e for e in events if e.to_status is TicketStatus.AWAITING_PLAN_APPROVAL)
    assert [i["id"] for i in approval.data["work_items"]] == ["W1", "W2", "W3"]
    await h.decide("T-1", "approved")
    await h.runner.run_until_idle()

    assert (await h.tickets.get("T-1")).status is TicketStatus.AWAITING_MERGE_APPROVAL
    # Rải phòng: đầu việc thứ k xoay danh sách worker của vai trò k nấc (W2 là đầu việc thứ hai).
    designer = company.role("designer").workers
    assert jobs["W1"]["room"] == company.role("coder").workers[0]
    assert jobs["W2"]["room"] == designer[1 % len(designer)]
    assert jobs["W2"]["role"] == "designer"
    # W3 thấy kết quả của W1 và W2 (W2 sửa ngoài phạm vi bị bỏ).
    assert jobs["W3"]["files"]["src/a.ts"] == "a2\n"
    assert jobs["W3"]["files"]["art/hero.pixel.json"] == "{}\n"

    branch = text_files(repo.files["ai/T-1"])
    assert branch["src/a.ts"] == "a2\n" and branch["src/main.ts"] == "main2\n"
    commits = [c["message"].split("\n")[0] for c in repo.commits["ai/T-1"]]
    assert set(commits[-4:-2]) == {"T-1/W1: làm W1", "T-1/W2: làm W2"}
    assert commits[-2:] == ["T-1/W3: làm W3", "T-1: cập nhật tài liệu kế hoạch (lần 1)"]
    doc = branch["docs/plans/T-1.md"]
    assert "| W2 | Sprite | designer | `art/` | - | xong" in doc
    assert "W2: bỏ thay đổi ngoài phạm vi: src/a.ts" in doc
    assert "graph TD\n  scene --> main" in branch["docs/architecture.md"]


async def test_failed_item_skips_dependents(company: Company) -> None:
    async def worker(request: httpx.Request) -> httpx.Response:
        job = json.loads(request.content)
        if "đầu việc W1" in job["prompt"]:
            return httpx.Response(200, json={"ok": False, "error": "hết hạn mức"})
        change = {"path": "art/x.json", "action": "create", "content": "1"}
        return httpx.Response(200, json={"ok": True, "result": "ok", "changes": [change]})

    plan_text = _plan(
        [
            {"id": "W1", "owns": ["src/a.ts"]},
            {"id": "W2", "owns": ["art/"]},
            {"id": "W3", "owns": ["src/main.ts"], "depends_on": ["W1"]},
        ],
        mermaid=None,
    )

    def coder_factory(gateway: ToolGateway) -> CliCoder:
        client = httpx.AsyncClient(transport=httpx.MockTransport(worker))
        runner = CliAgentRunner(
            company, TaskTokens(), CliSettings(WORKERS, "worker-secret", "u", 0.01, client)
        )
        return CliCoder(runner, gateway, default_project=PROJECT)

    h = make_harness(company, planner=lambda gw: _PlanText(plan_text), coder=coder_factory)
    await h.new_ticket(labels=["small-fix"])
    await h.runner.run_until_idle()

    doc = text_files(h.gitlab.project(PROJECT).files["ai/T-1"])["docs/plans/T-1.md"]
    assert "| W1 |" in doc and "lỗi" in doc
    assert "bỏ qua: đầu việc trước lỗi" in doc  # W3 phụ thuộc W1
    assert "docs/architecture.md" not in h.gitlab.project(PROJECT).files["ai/T-1"]


async def _run_with_existing_architecture(company: Company, labels: list[str]) -> str:
    async def worker(request: httpx.Request) -> httpx.Response:
        change = {"path": "src/a.ts", "action": "update", "content": "a2\n"}
        result = "Xong.\nTÓM TẮT: sửa a"
        return httpx.Response(200, json={"ok": True, "result": result, "changes": [change]})

    plan_text = _plan([{"id": "W1", "owns": ["src/a.ts"]}], mermaid="graph TD\n  moi --> rut_gon")

    def coder_factory(gateway: ToolGateway) -> CliCoder:
        client = httpx.AsyncClient(transport=httpx.MockTransport(worker))
        runner = CliAgentRunner(
            company, TaskTokens(), CliSettings(WORKERS, "worker-secret", "u", 0.01, client)
        )
        return CliCoder(runner, gateway, default_project=PROJECT)

    h = make_harness(company, planner=lambda gw: _PlanText(plan_text), coder=coder_factory)
    repo = h.gitlab.project(PROJECT)
    repo.files["main"].update({"src/a.ts": "a1\n", "docs/architecture.md": "# Bản đồ chung\n"})
    await h.new_ticket(labels=labels)
    await h.runner.run_until_idle()
    await h.decide("T-1", "approved")
    await h.runner.run_until_idle()
    branch = text_files(repo.files["ai/T-1"])
    assert branch["src/a.ts"] == "a2\n" and "docs/plans/T-1.md" in branch
    return branch["docs/architecture.md"]


async def test_existing_architecture_kept_for_ordinary_ticket(company: Company) -> None:
    # Sơ đồ rút gọn của một task lẻ không được xóa bản đồ chung repo đã có.
    assert await _run_with_existing_architecture(company, []) == "# Bản đồ chung\n"


async def test_architecture_label_overwrites_existing_map(company: Company) -> None:
    doc = await _run_with_existing_architecture(company, ["architecture"])
    assert "graph TD\n  moi --> rut_gon" in doc


async def test_runners_process_tickets_concurrently(company: Company) -> None:
    active = 0
    peak = 0

    class SlowPlanner:
        async def plan(self, ticket: Ticket, feedback: str | None, requirements: str = "") -> str:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.2)
            active -= 1
            return "kế hoạch"

    h = make_harness(company, planner=lambda gw: SlowPlanner())
    await h.new_ticket("T-1")
    await h.new_ticket("T-2")
    second = Runner(h.runner._graph, h.tickets, lease_seconds=30)  # noqa: SLF001
    await asyncio.gather(h.runner.run_once(), second.run_once())
    assert peak == 2
    statuses = {(await h.tickets.get(t)).status for t in ("T-1", "T-2")}
    assert statuses == {TicketStatus.AWAITING_PLAN_APPROVAL}
