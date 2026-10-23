"""Ticket tài liệu (nhãn docs): Researcher, BA, Tech Lead viết tài liệu; Reviewer chấm tài liệu."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from aico.agents import (
    DOCS_LABEL,
    DOCS_REVIEW_BRIEF,
    DOCS_ROLES,
    CliAgentRunner,
    CliCoder,
    CliReviewer,
    CliSettings,
    DocsPlanner,
    LlmPlanner,
)
from aico.core import Ticket, TicketStatus
from aico.llm_relay import TaskTokens
from aico.orchestrator import LabelPlanner, ScriptedReviewer
from aico.orchestrator.workplan import parse_work_plan
from aico.registry import Company
from harness import PROJECT, make_harness
from test_ba_tester_flow import FakeAnalyst
from test_cli_agent import WORKERS, _load_worker_server

S = TicketStatus

PLAN = """Nghiên cứu thể loại rồi viết GDD và lộ trình.

```json
{"items": [
  {"id": "W1", "title": "Nghiên cứu game merge", "role": "researcher",
   "owns": ["docs/research/genre.md"], "acceptance": "có nguồn"},
  {"id": "W2", "title": "Nghiên cứu phong cách tu tiên", "role": "researcher",
   "owns": ["docs/research/style.md"]},
  {"id": "W3", "title": "Viết GDD", "role": "ba", "owns": ["docs/product/"],
   "depends_on": ["W1", "W2"]},
  {"id": "W4", "title": "Lộ trình và sprint 1", "role": "tech_lead",
   "owns": ["docs/delivery/", "docs/README.md"], "depends_on": ["W3"]}
]}
```"""

FILES = {
    "W1": "docs/research/genre.md",
    "W2": "docs/research/style.md",
    "W3": "docs/product/GDD.md",
    "W4": "docs/delivery/roadmap.md",
}


def _worker(jobs: list[dict[str, Any]], verdicts: list[str]) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":  # /status: không công bố hạn mức
            return httpx.Response(200, json={"state": "idle"})
        job = json.loads(request.content)
        job["worker"] = request.url.host
        jobs.append(job)
        if job["mode"] == "read":  # Tech Lead lập kế hoạch
            return httpx.Response(200, json={"ok": True, "result": PLAN})
        if job["mode"] == "review":
            return httpx.Response(200, json={"ok": True, "result": verdicts.pop(0)})
        item = job["prompt"].split("đầu việc ", 1)[1].split(" ", 1)[0]
        path = FILES.get(item, "docs/delivery/sprint-01.md")  # FIX: sửa theo góp ý
        change = {"path": path, "action": "create", "content": f"# {item}\n"}
        return httpx.Response(
            200, json={"ok": True, "result": f"TÓM TẮT: viết {path}", "changes": [change]}
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_docs_ticket_runs_research_ba_tech_lead_and_skips_ba_step_and_tester(
    company: Company,
) -> None:
    jobs: list[dict[str, Any]] = []
    verdicts = ["Roadmap thiếu checklist.\nKẾT LUẬN: CẦN SỬA", "Đủ mục.\nKẾT LUẬN: ĐẠT"]
    runner = CliAgentRunner(
        company,
        TaskTokens(),
        CliSettings(WORKERS, "worker-secret", "u", 0.01, _worker(jobs, verdicts)),
    )
    analyst, tester = FakeAnalyst(), ScriptedReviewer()
    h = make_harness(
        company,
        planner=lambda gw: LabelPlanner(LlmPlanner(runner), {DOCS_LABEL: DocsPlanner(runner)}),
        coder=lambda gw: CliCoder(runner, gw, default_project=PROJECT),
        reviewer_agent=lambda gw: CliReviewer(
            runner, gw, label_briefs={DOCS_LABEL: DOCS_REVIEW_BRIEF}
        ),
        analyst=analyst,
        tester=tester,
        label_item_roles={DOCS_LABEL: DOCS_ROLES},
        test_skip_labels=(DOCS_LABEL,),
    )
    h.gitlab.project(PROJECT).files["main"]["README.md"] = "# game\n"
    await h.new_ticket(labels=[DOCS_LABEL], body="Nghiên cứu và lập kế hoạch game")

    await h.runner.run_until_idle()
    assert (await h.tickets.get("T-1")).status is S.AWAITING_PLAN_APPROVAL  # docs: luôn duyệt
    assert analyst.calls == []
    events = await h.tickets.events("T-1")
    waiting = next(e for e in events if e.to_status is S.AWAITING_PLAN_APPROVAL)
    assert [i["role"] for i in waiting.data["work_items"]] == [
        "researcher",
        "researcher",
        "ba",
        "tech_lead",
    ]
    assert "Chuẩn tài liệu (aico thêm)" in waiting.data["plan"]
    plan_prompt = jobs[0]["prompt"]
    assert "docs/research/" in plan_prompt and "role researcher" in plan_prompt

    await h.decide("T-1", "approved")
    await h.runner.run_until_idle()

    assert (await h.tickets.get("T-1")).status is S.AWAITING_MERGE_APPROVAL
    items = [j for j in jobs if j["mode"] == "write"]
    by_role = {j["role"]: j for j in items}
    # Chỉ Researcher được lên web, và chỉ chạy ở phòng có tìm kiếm web (không phải cursor).
    assert by_role["researcher"]["web"] is True and by_role["ba"]["web"] is False
    assert all(j["worker"] != "cursor" for j in items if j["role"] == "researcher")
    # Đầu việc chạy theo phụ thuộc: hai researcher trước BA, BA trước tech_lead.
    order = [j["role"] for j in items]
    assert order[:4] == ["researcher", "researcher", "ba", "tech_lead"]
    # Vòng sửa sau review do tech_lead làm (vai trò đầu của DOCS_ROLES), không phải coder.
    assert order[4] == "tech_lead" and "Roadmap thiếu checklist" in items[4]["prompt"]
    reviews = [j for j in jobs if j["mode"] == "review"]
    assert len(reviews) == 2 and "chấm nội dung, không chạy build/test" in reviews[0]["prompt"]
    assert tester.requirements_seen == []  # không qua Tester
    files = h.gitlab.project(PROJECT).files["ai/T-1"]
    assert files["docs/research/genre.md"] == "# W1\n" and "docs/delivery/roadmap.md" in files
    statuses = [e.to_status for e in await h.tickets.events("T-1") if e.kind == "status"]
    assert S.IN_TESTING not in statuses


def test_docs_roles_are_accepted_only_for_docs_plans() -> None:
    work = parse_work_plan(PLAN, DOCS_ROLES)
    assert [i.role for i in work.items] == ["researcher", "researcher", "ba", "tech_lead"]
    assert work.architecture is None  # kế hoạch tài liệu không ghi đè docs/architecture.md
    code = parse_work_plan(PLAN, ("coder", "designer"))
    assert {i.role for i in code.items} == {"coder"}


async def test_reviewer_uses_default_brief_without_docs_label(company: Company) -> None:
    prompts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"state": "idle"})
        prompts.append(json.loads(request.content)["prompt"])
        return httpx.Response(200, json={"ok": True, "result": "KẾT LUẬN: ĐẠT"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    runner = CliAgentRunner(company, TaskTokens(), CliSettings(WORKERS, "w", "u", 0.01, client))
    h = make_harness(company)
    h.gitlab.project(PROJECT).files["main"]["README.md"] = "# game\n"
    reviewer = CliReviewer(runner, h.gateway, label_briefs={DOCS_LABEL: DOCS_REVIEW_BRIEF})
    mr = {"iid": 1, "source_branch": "main", "target_branch": "main"}
    await reviewer.review(Ticket(id="T-9", title="Sửa nút", project=PROJECT), mr)
    await reviewer.review(Ticket(id="T-8", title="Kế hoạch", project=PROJECT, labels=["Docs"]), mr)
    assert "lỗi logic" in prompts[0] and "chấm nội dung" not in prompts[0]
    assert "chấm nội dung" in prompts[1]  # nhãn không phân biệt hoa thường


def test_worker_web_jobs_open_only_web_tools(tmp_path: Path) -> None:
    ws = _load_worker_server()
    base = {"prompt": "p", "mcp_url": "u", "mcp_token": "t"}
    claude, _ = ws.build_command({**base, "mode": "read", "web": True}, "claude", tmp_path)
    assert "--tools=WebSearch,WebFetch" in claude and "--tools=" not in claude
    allowed = claude[claude.index("--allowedTools") + 1]
    assert "WebSearch" in allowed and "Bash" not in allowed
    claude, _ = ws.build_command({**base, "mode": "write", "web": True}, "claude", tmp_path)
    allowed = claude[claude.index("--allowedTools") + 1]
    assert "WebFetch" in allowed and "Edit" in allowed
    codex, _ = ws.build_command({**base, "mode": "write", "web": True}, "codex", tmp_path)
    assert 'web_search="live"' in codex
    # Không có cờ web: giữ nguyên như cũ.
    claude, _ = ws.build_command({**base, "mode": "read"}, "claude", tmp_path)
    assert "--tools=" in claude and "WebSearch" not in " ".join(claude)
    codex, _ = ws.build_command({**base, "mode": "read"}, "codex", tmp_path)
    assert not any("web_search" in part for part in codex)
