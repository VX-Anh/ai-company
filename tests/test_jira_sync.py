from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta

import httpx

from aico.core import Artifact, Ticket, TicketSource, TicketStatus, utcnow
from aico.jira import (
    HttpJiraClient,
    JiraIssue,
    JiraSync,
    LabeledIssue,
    adf_text,
    parse_sprint_tasks,
)
from aico.tickets import InMemoryTicketStore

S = TicketStatus
REPO = "VX-Anh/linh-hoa-vien"

SPRINT_02 = """# Sprint 02

| Mã | Việc | Vai trò | Ước lượng |
|---|---|---|---|
| S02-T1 | **Đưa bản thử lên itch.io:** build tĩnh, đường dẫn tương đối | coder | 1 |
| S02-T2 | Funnel event `load_start` | coder | 0.5 |

## Truy vết

| Task | US |
|---|---|
| S02-T1 | US-10 |
"""


@dataclass
class FakeJira:
    base_url: str = "https://x.atlassian.net"
    issues: dict[str, JiraIssue] = field(default_factory=dict)
    summaries: dict[str, str] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)
    parents: dict[str, str | None] = field(default_factory=dict)
    epics: dict[str, str] = field(default_factory=dict)
    sprint: tuple[int, str] | None = None
    in_sprint: list[str] = field(default_factory=list)
    comments: list[tuple[str, str]] = field(default_factory=list)
    links: set[tuple[str, str]] = field(default_factory=set)
    labeled: list[LabeledIssue] = field(default_factory=list)
    calls: int = 0

    def add(self, key: str, summary: str, description: str = "", status: str = "To Do") -> None:
        self.issues[key] = JiraIssue(key, status, ("aico",))
        self.summaries[key] = summary
        self.descriptions[key] = description

    async def find_by_ticket(self, project_key: str, ticket_id: str) -> JiraIssue | None:
        self.calls += 1
        for key, text in self.descriptions.items():
            if key.startswith(project_key + "-") and ticket_id in text:
                return self.issues[key]
        return None

    async def find_by_prefix(
        self, project_key: str, prefix: str, *, epic: bool = False
    ) -> str | None:
        if epic:
            return self.epics.get(prefix)
        return next((k for k, s in self.summaries.items() if s.startswith(prefix)), None)

    async def get(self, key: str) -> JiraIssue:
        self.calls += 1
        return self.issues[key]

    async def create(
        self, project_key: str, summary: str, description: str, *, parent: str | None
    ) -> JiraIssue:
        self.calls += 1
        key = f"{project_key}-{len(self.issues) + 1}"
        self.add(key, summary, description)
        self.parents[key] = parent
        return self.issues[key]

    async def active_sprint(self, project_key: str) -> tuple[int, str] | None:
        return self.sprint

    async def add_to_sprint(self, sprint_id: int, key: str) -> None:
        self.in_sprint.append(key)

    async def transition(self, key: str, status: str) -> bool:
        self.calls += 1
        old = self.issues[key]
        self.issues[key] = JiraIssue(key, status, old.labels)
        return True

    async def set_labels(self, key: str, add: set[str], remove: set[str]) -> None:
        old = self.issues[key]
        labels = (set(old.labels) | add) - remove
        self.issues[key] = JiraIssue(key, old.status, tuple(sorted(labels)))

    async def comment(self, key: str, text: str) -> None:
        self.comments.append((key, text))

    async def link(self, key: str, url: str, title: str) -> None:
        self.links.add((key, url))

    async def list_labeled(self, project_keys: Iterable[str], label: str) -> list[LabeledIssue]:
        keys = set(project_keys)
        return [i for i in self.labeled if i.project_key in keys and label in i.labels]


async def _ticket(store: InMemoryTicketStore, tid: str, title: str, **kw: object) -> Ticket:
    fields: dict[str, object] = {"project": REPO, **kw}
    return await store.create(Ticket(id=tid, title=title, **fields), actor="test")  # type: ignore[arg-type]


async def test_creates_issue_under_epic_and_follows_status() -> None:
    store = InMemoryTicketStore()
    jira = FakeJira(epics={"S02": "LHV-2"}, sprint=(7, "LHV Sprint 02"))
    sync = JiraSync(jira, store, {REPO: "LHV"})
    await _ticket(store, "T-1", "S02-T1: Đưa bản thử lên itch.io")
    await _ticket(store, "T-other", "Việc repo khác", project="VX-Anh/aico-office")

    assert await sync.sync_once() == 1
    ticket = await store.get("T-1")
    assert [a.ref for a in ticket.artifacts if a.kind == "jira"] == ["LHV-1"]
    assert jira.parents["LHV-1"] == "LHV-2"
    assert jira.in_sprint == ["LHV-1"]
    assert "T-1" in jira.descriptions["LHV-1"]
    assert jira.comments == []  # lần đầu thấy ticket: chỉ đồng bộ, không bình luận

    for status in (S.TRIAGED, S.PLANNED, S.AWAITING_PLAN_APPROVAL):
        await store.transition("T-1", status, actor="test")
    await sync.sync_once()
    issue = jira.issues["LHV-1"]
    assert issue.status == "In Progress"
    assert "cho-duyet-ke-hoach" in issue.labels
    assert jira.comments[-1][0] == "LHV-1"
    assert "chờ người duyệt" in jira.comments[-1][1]

    await store.transition("T-1", S.IN_PROGRESS, actor="test")
    mr = Artifact(kind="merge_request", ref="VX-Anh/linh-hoa-vien!10", url="https://gh/pull/10")
    await store.add_artifact("T-1", mr, actor="test")
    await sync.sync_once()
    assert "cho-duyet-ke-hoach" not in jira.issues["LHV-1"].labels
    assert jira.links == {("LHV-1", "https://gh/pull/10")}

    calls = jira.calls
    assert await sync.sync_once() == 0  # không đổi gì thì không gọi Jira
    assert jira.calls == calls


async def test_reuses_issue_created_by_hand_and_skips_old_tickets() -> None:
    store = InMemoryTicketStore()
    jira = FakeJira()
    jira.add("LHV-14", "S01-T8: Đối chiếu", "Ticket aico: T-eb0e92c3 (đang làm)", "In Progress")
    await _ticket(store, "T-eb0e92c3", "S01-T8: Đối chiếu cuối sprint")
    sync = JiraSync(jira, store, {REPO: "LHV"}, since=utcnow() - timedelta(minutes=1))

    await sync.sync_once()
    assert len(jira.issues) == 1
    assert [a.ref for a in (await store.get("T-eb0e92c3")).artifacts] == ["LHV-14"]
    assert jira.issues["LHV-14"].status == "To Do"  # ticket mới tạo nên quay về To Do

    late = JiraSync(jira, store, {REPO: "LHV"}, since=utcnow() + timedelta(minutes=1))
    assert await late.sync_once() == 0


async def test_ticket_reuses_backlog_issue_by_task_code() -> None:
    store = InMemoryTicketStore()
    jira = FakeJira()
    jira.add("LHV-20", "S02-T1: Đưa bản thử lên itch.io", "Nguồn: sprint-02.md")
    await _ticket(store, "T-9", "S02-T1: Bản thử itch.io (làm theo sprint-02)")
    await JiraSync(jira, store, {REPO: "LHV"}).sync_once()
    assert len(jira.issues) == 1
    assert [a.ref for a in (await store.get("T-9")).artifacts] == ["LHV-20"]


async def test_sprint_task_from_other_sprint_stays_in_backlog() -> None:
    store = InMemoryTicketStore()
    jira = FakeJira(sprint=(3, "LHV Sprint 01"))
    await _ticket(store, "T-2", "S02-T3: Funnel event")
    await JiraSync(jira, store, {REPO: "LHV"}).sync_once()
    assert jira.in_sprint == []


def test_parse_sprint_tasks_takes_first_table_row_per_code() -> None:
    tasks = parse_sprint_tasks(SPRINT_02, 2)
    assert [t.code for t in tasks] == ["S02-T1", "S02-T2"]
    assert tasks[0].summary == "S02-T1: Đưa bản thử lên itch.io"
    assert "Vai trò: coder" in tasks[0].description
    assert tasks[1].summary == "S02-T2: Funnel event load_start"


async def test_backlog_created_on_start_and_after_docs_ticket_merges() -> None:
    store = InMemoryTicketStore()
    jira = FakeJira(epics={"S01": "LHV-1", "S02": "LHV-2"}, sprint=(3, "LHV Sprint 01"))
    jira.add("LHV-7", "S01-T1: Dựng khung dự án")
    files = {
        "docs/delivery/sprint-01.md": "| Mã | Việc |\n|---|---|\n| S01-T1 | **Dựng khung:** x |"
    }

    async def read_file(repo: str, path: str) -> str | None:
        assert repo == REPO
        return files.get(path)

    sync = JiraSync(jira, store, {REPO: "LHV"}, read_file=read_file)
    await sync.sync_once()
    assert set(jira.issues) == {"LHV-7"}  # S01-T1 đã có issue, không tạo trùng

    await _ticket(store, "T-d", "S01-T8: Lập sprint-02", labels=["docs"])
    await sync.sync_once()
    files["docs/delivery/sprint-02.md"] = SPRINT_02
    for status in (S.TRIAGED, S.PLANNED, S.IN_PROGRESS, S.IN_REVIEW):
        await store.transition("T-d", status, actor="test")
    await sync.sync_once()
    assert not any(s.startswith("S02") for s in jira.summaries.values())  # chưa merge

    for status in (S.AWAITING_MERGE_APPROVAL, S.MERGING, S.DONE):
        await store.transition("T-d", status, actor="test")
    await sync.sync_once()  # thấy ticket docs xong, hẹn đọc lại
    await sync.sync_once()
    s02 = {k: s for k, s in jira.summaries.items() if s.startswith("S02")}
    assert sorted(s02.values()) == [
        "S02-T1: Đưa bản thử lên itch.io",
        "S02-T2: Funnel event load_start",
    ]
    assert all(jira.parents[k] == "LHV-2" for k in s02)
    assert not set(s02) & set(jira.in_sprint)  # sprint 01 đang chạy: S02 nằm backlog


async def test_intake_turns_labeled_issue_into_ticket_once() -> None:
    store = InMemoryTicketStore()
    jira = FakeJira()
    jira.add("LHV-30", "Sửa nút Giao bị che trên điện thoại", "", "To Do")
    jira.labeled = [
        LabeledIssue(
            "LHV-30",
            "LHV",
            "Sửa nút Giao bị che trên điện thoại",
            "Màn 360×640.",
            ("ai-company", "low-risk"),
        ),
        LabeledIssue("OTHER-1", "OTHER", "Không ánh xạ", "", ("ai-company",)),
    ]
    sync = JiraSync(jira, store, {REPO: "LHV"}, intake_label="ai-company")

    await sync.sync_once()
    await sync.sync_once()
    tickets = await store.list()
    assert len(tickets) == 1
    t = tickets[0]
    assert (t.source, t.source_ref, t.project) == (TicketSource.JIRA, "jira:LHV-30", REPO)
    assert t.labels == ["low-risk"]
    assert t.body == "Màn 360×640."
    assert [a.ref for a in t.artifacts if a.kind == "jira"] == ["LHV-30"]
    assert len(jira.issues) == 1  # sync dùng lại LHV-30, không tạo issue mới


def test_adf_text_keeps_paragraph_breaks() -> None:
    doc = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Dòng 1"}]},
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "ý A"}]}
                        ],
                    }
                ],
            },
        ],
    }
    assert adf_text(doc).split() == ["Dòng", "1", "ý", "A"]
    assert adf_text(doc).count("\n") >= 2
    assert adf_text(None) == ""


async def test_http_client_picks_transition_by_target_status() -> None:
    seen: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        if request.url.path.endswith("/transitions") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "transitions": [
                        {"id": "11", "to": {"name": "To Do"}},
                        {"id": "31", "to": {"name": "Done"}},
                    ]
                },
            )
        return httpx.Response(204)

    client = HttpJiraClient(
        "https://x.atlassian.net/", "a@b.c", "tok", transport=httpx.MockTransport(handler)
    )
    assert await client.transition("LHV-1", "done")
    assert seen[-1] == (
        "POST",
        "/rest/api/3/issue/LHV-1/transitions",
        {"transition": {"id": "31"}},
    )
    assert not await client.transition("LHV-1", "In Review")
    await client.aclose()


async def test_http_client_prefix_search_filters_fuzzy_matches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        jql = json.loads(request.content)["jql"]
        assert 'summary ~ "\\"S01\\""' in jql and "issuetype = Epic" in jql
        issues = [
            {"key": "LHV-9", "fields": {"summary": "S010 — không phải"}},
            {"key": "LHV-1", "fields": {"summary": "S01 — Khung dự án"}},
        ]
        return httpx.Response(200, json={"issues": issues})

    client = HttpJiraClient("https://x", "a", "t", transport=httpx.MockTransport(handler))
    assert await client.find_by_prefix("LHV", "S01", epic=True) == "LHV-1"
    await client.aclose()
