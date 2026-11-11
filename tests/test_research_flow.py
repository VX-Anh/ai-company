"""Ticket nghiên cứu (nhãn research, Phòng R&D): Research Lead chia việc, researcher viết thẻ,
Fact Checker kiểm chứng; không qua BA, không duyệt kế hoạch, không qua Tester."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import yaml

from aico.agents import (
    CARDS_DIR,
    RESEARCH_LABEL,
    RESEARCH_REVIEW_BRIEF,
    RESEARCH_REVIEW_ROUNDS,
    RESEARCH_ROLES,
    CliAgentRunner,
    CliCoder,
    CliReviewer,
    CliSettings,
    LlmPlanner,
    ResearchPlanner,
)
from aico.agents.research import strip_mermaid
from aico.core import ActionLevel, Ticket, TicketRisk, TicketStatus
from aico.llm_relay import TaskTokens
from aico.orchestrator import (
    LabelPlanner,
    LabelReviewer,
    ReviewResult,
    RuleTriage,
    ScriptedReviewer,
)
from aico.orchestrator.workplan import ALL_FILES, WorkItem, clamp_owns
from aico.registry import Company, load_company
from harness import PROJECT, make_harness
from test_ba_tester_flow import FakeAnalyst
from test_cli_agent import WORKERS

S = TicketStatus

PLAN = """Hai câu hỏi về prompt caching.

```mermaid
flowchart LR
  A --> B
```

```json
{"items": [
  {"id": "W1", "title": "Tạo mới: prompt caching là gì", "role": "researcher",
   "owns": ["cards/technique/prompt-caching.md"], "acceptance": "2 nguồn chính thức"},
  {"id": "W2", "title": "Cập nhật thẻ tool/claude-code", "role": "coder",
   "owns": ["cards/tool/claude-code.md"]}
]}
```"""

FILES = {"W1": "cards/technique/prompt-caching.md", "W2": "cards/tool/claude-code.md"}


def _company(company_copy: Path) -> Company:
    # Harness chỉ có một repo giả (aico-office): cho vai trò R&D dùng repo đó trong test.
    for role in ("research_lead", "fact_checker"):
        path = company_copy / "roles" / f"{role}.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["tools"][0]["constraints"]["repos"].append(PROJECT)
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    # Luật research = low chỉ áp cho kho tri thức: thêm repo giả vào luật đó.
    policy = company_copy / "policies" / "default.yaml"
    data = yaml.safe_load(policy.read_text(encoding="utf-8"))
    for rule in data["risk_rules"]:
        if "research" in rule.get("labels", []):
            rule["projects"].append(PROJECT)
    policy.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return load_company(company_copy)


def _worker(jobs: list[dict[str, Any]], verdicts: list[str]) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"state": "idle"})
        job = json.loads(request.content)
        job["worker"] = request.url.host
        jobs.append(job)
        if job["mode"] == "read":
            return httpx.Response(200, json={"ok": True, "result": PLAN})
        if job["mode"] == "review":
            return httpx.Response(200, json={"ok": True, "result": verdicts.pop(0)})
        item = job["prompt"].split("đầu việc ", 1)[1].split(" ", 1)[0]
        path = FILES.get(item, FILES["W1"])  # FIX: sửa theo góp ý
        change = {"path": path, "action": "create", "content": f"# {item}\n"}
        return httpx.Response(
            200, json={"ok": True, "result": f"TÓM TẮT: viết {path}", "changes": [change]}
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_research_ticket_skips_plan_approval_ba_tester_and_uses_fact_checker(
    company_copy: Path,
) -> None:
    company = _company(company_copy)
    jobs: list[dict[str, Any]] = []
    verdicts = [
        "S1 không có trích dẫn trên trang.\nKẾT LUẬN: CẦN SỬA",
        "Nguồn khớp.\nKẾT LUẬN: ĐẠT",
        "Nguồn khớp.\nKẾT LUẬN: ĐẠT",
    ]
    runner = CliAgentRunner(
        company,
        TaskTokens(),
        CliSettings(WORKERS, "worker-secret", "u", 0.01, _worker(jobs, verdicts)),
    )
    analyst, tester = FakeAnalyst(), ScriptedReviewer()
    h = make_harness(
        company,
        planner=lambda gw: LabelPlanner(
            LlmPlanner(runner), {RESEARCH_LABEL: ResearchPlanner(runner)}
        ),
        coder=lambda gw: CliCoder(runner, gw, default_project=PROJECT),
        reviewer_agent=lambda gw: LabelReviewer(
            CliReviewer(runner, gw),
            {
                RESEARCH_LABEL: CliReviewer(
                    runner, gw, role_id="fact_checker", brief=RESEARCH_REVIEW_BRIEF, plan_rule=None
                )
            },
        ),
        analyst=analyst,
        tester=tester,
        label_item_roles={RESEARCH_LABEL: RESEARCH_ROLES},
        test_skip_labels=(RESEARCH_LABEL,),
        label_scopes={RESEARCH_LABEL: (CARDS_DIR,)},
    )
    h.gitlab.project(PROJECT).files["main"]["INDEX.md"] = "# Mục lục\n"
    await h.new_ticket(labels=[RESEARCH_LABEL], body="Prompt caching dùng thế nào cho agent?")

    await h.runner.run_until_idle()

    # Không duyệt kế hoạch: đi thẳng tới chờ duyệt merge (= duyệt nạp tri thức).
    assert (await h.tickets.get("T-1")).status is S.AWAITING_MERGE_APPROVAL
    assert analyst.calls == [] and tester.requirements_seen == []
    plan_job = jobs[0]
    assert plan_job["role"] == "research_lead" and plan_job["web"] is False
    assert "INDEX.md" in plan_job["prompt"] and "KHÔNG viết khối" in plan_job["prompt"]
    events = await h.tickets.events("T-1")
    items = [j for j in jobs if j["mode"] == "write"]
    # Mọi đầu việc giao cho researcher (role lạ trong kế hoạch bị đổi), có web, không ở cursor.
    assert {j["role"] for j in items} == {"researcher"}
    assert all(j["web"] is True and j["worker"] != "cursor" for j in items)
    reviews = [j for j in jobs if j["mode"] == "review"]
    assert [j["role"] for j in reviews] == ["fact_checker", "fact_checker"]
    assert reviews[0]["worker"] == "codex"  # ngược thứ tự với researcher (claude trước)
    assert "kiểm chứng nội dung, không chấm code" in reviews[0]["prompt"]
    # Ticket research không có bước người duyệt kế hoạch: không nói "kế hoạch đã được người duyệt".
    assert "kế hoạch đã được người duyệt" not in reviews[0]["prompt"]
    # Vòng sửa sau Fact Checker do researcher làm, kèm góp ý, và vẫn chỉ được sửa trong cards/
    # (vòng sửa mặc định cho sửa mọi file).
    assert "S1 không có trích dẫn" in items[-1]["prompt"]
    assert f"CHỈ được sửa/tạo các file khớp: {CARDS_DIR}." in items[-1]["prompt"]
    files = h.gitlab.project(PROJECT).files["ai/T-1"]
    assert FILES["W1"] in files and FILES["W2"] in files
    # Kế hoạch có khối mermaid nhưng không được ghi thành docs/architecture.md của kho.
    assert "docs/architecture.md" not in files
    statuses = [e.to_status for e in events if e.kind == "status"]
    assert S.AWAITING_PLAN_APPROVAL not in statuses and S.IN_TESTING not in statuses

    # Người duyệt yêu cầu sửa: researcher làm lại, Fact Checker chấm lại, rồi duyệt nạp.
    await h.decide("T-1", "changes_requested", "Thêm nguồn tài liệu chính thức")
    await h.runner.run_until_idle()
    assert (await h.tickets.get("T-1")).status is S.AWAITING_MERGE_APPROVAL
    assert (
        "Thêm nguồn tài liệu chính thức" in [j for j in jobs if j["mode"] == "write"][-1]["prompt"]
    )
    await h.decide("T-1", "approved")
    await h.runner.run_until_idle()
    assert (await h.tickets.get("T-1")).status is S.DONE


async def test_label_reviewer_falls_back_to_default() -> None:
    default, special = ScriptedReviewer(), ScriptedReviewer()
    reviewer = LabelReviewer(default, {RESEARCH_LABEL: special})
    assert reviewer.pick(Ticket(id="T-1", title="x")) is default
    assert reviewer.pick(Ticket(id="T-2", title="x", labels=["Research"])) is special


def test_strip_mermaid_removes_only_mermaid_blocks() -> None:
    text = strip_mermaid(PLAN)
    assert "flowchart" not in text and '"items"' in text


async def test_research_label_is_low_risk_even_with_other_labels(company: Company) -> None:
    triage = RuleTriage(company.policy, "rnd")
    kb = "VX-Anh/aico-knowledge"
    t = Ticket(id="T-1", title="Tìm hiểu migration", labels=["research"], project=kb)
    assert (await triage.triage(t)).risk is TicketRisk.LOW
    # Ở repo khác, research không bỏ được bước duyệt kế hoạch.
    other = t.model_copy(update={"project": PROJECT})
    assert (await triage.triage(other)).risk is TicketRisk.HIGH  # rơi xuống luật từ khoá migration
    plain = Ticket(id="T-2", title="Tìm hiểu prompt", labels=["research"], project=PROJECT)
    assert (await triage.triage(plain)).risk is TicketRisk.MEDIUM
    # Nhãn security thắng research.
    sec = t.model_copy(update={"labels": ["research", "security"]})
    assert (await triage.triage(sec)).risk is TicketRisk.HIGH


def test_rnd_department_config(company: Company) -> None:
    assert company.org.departments[-1].id == "rnd"
    for role in ("research_lead", "researcher", "fact_checker"):
        spec = company.role(role)
        assert spec.department == "rnd" and spec.max_risk is ActionLevel.L0, role
    researcher, checker = company.role("researcher"), company.role("fact_checker")
    assert checker.web and researcher.web and not company.role("research_lead").web
    assert checker.workers[0] != researcher.workers[0]  # kiểm chứng ở phòng khác người viết
    # Bước Git của luồng nhãn chạy dưới vai trò SOP: các vai trò này phải với tới kho tri thức.
    for role in ("coder", "reviewer", "merge_bot", "researcher", "fact_checker", "research_lead"):
        grant = company.role(role).grant_for("gitlab")
        assert grant is not None and "VX-Anh/aico-knowledge" in grant.constraints.repos, role


async def test_research_gets_more_review_rounds_than_code_tickets(company: Company) -> None:
    fails = [ReviewResult(False, f"vòng {i}: trích dẫn sai") for i in range(1, 5)]
    rounds = {RESEARCH_LABEL: RESEARCH_REVIEW_ROUNDS}
    # Ticket research: 4 lần CẦN SỬA vẫn còn lượt, lần 5 đạt thì tới chờ duyệt merge.
    h = make_harness(company, reviewer=ScriptedReviewer(list(fails)), label_review_rounds=rounds)
    # Repo giả của harness không phải kho tri thức: low-risk để bỏ duyệt kế hoạch như ở kho.
    await h.new_ticket(labels=[RESEARCH_LABEL, "low-risk"])
    await h.runner.run_until_idle()
    assert (await h.tickets.get("T-1")).status is S.AWAITING_MERGE_APPROVAL
    # Ticket không có nhãn: vẫn theo giới hạn chung 3 vòng của SOP.
    h = make_harness(company, reviewer=ScriptedReviewer(list(fails)), label_review_rounds=rounds)
    await h.new_ticket(labels=["low-risk"])
    await h.runner.run_until_idle()
    t = await h.tickets.get("T-1")
    assert t.status is S.ESCALATED
    assert company.sop("fix_ticket").limits.max_review_rounds == 3


def test_clamp_owns_keeps_items_inside_scope() -> None:
    scope = (CARDS_DIR,)
    fix = WorkItem("FIX", "Sửa theo góp ý", "researcher", (ALL_FILES,))
    assert clamp_owns(fix, scope).owns == scope
    mixed = WorkItem("W1", "t", "researcher", ("cards/tool/x.md", "INDEX.md", ".github/a.yml"))
    assert clamp_owns(mixed, scope).owns == ("cards/tool/x.md",)
    outside = WorkItem("W2", "t", "researcher", ("scripts/check_cards.py",))
    assert clamp_owns(outside, scope).owns == scope
