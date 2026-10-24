"""Nối aico với Jira. aico là nguồn sự thật; Jira là nơi người theo dõi và giao việc.

- Đồng bộ: ticket -> issue (trạng thái, nhãn chờ người, link PR, bình luận ở các mốc).
- Backlog: task trong `docs/delivery/sprint-NN.md` của repo -> issue dưới epic sprint, lúc khởi
  động và sau mỗi ticket nhãn `docs` merge xong (tài liệu sprint mới chỉ có sau bước đó).
- Intake: issue Jira có nhãn intake (mặc định `ai-company`) chưa xong -> ticket aico.

Polling như intake GitHub vì PC không nhận webhook.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import httpx

from aico.core import Artifact, Ticket, TicketSource, TicketStatus
from aico.intake import new_ticket_id
from aico.tickets import TicketStore

log = logging.getLogger(__name__)

S = TicketStatus

# Trạng thái aico -> trạng thái Jira (workflow mặc định To Do / In Progress / Done).
# FAILED giữ In Progress kèm nhãn để người thấy, không đóng âm thầm.
JIRA_STATUS: dict[TicketStatus, str] = {
    S.NEW: "To Do",
    S.TRIAGED: "To Do",
    S.PLANNED: "To Do",
    S.AWAITING_PLAN_APPROVAL: "In Progress",
    S.IN_PROGRESS: "In Progress",
    S.IN_REVIEW: "In Progress",
    S.IN_TESTING: "In Progress",
    S.AWAITING_MERGE_APPROVAL: "In Progress",
    S.MERGING: "In Progress",
    S.ESCALATED: "In Progress",
    S.DONE: "Done",
    S.FAILED: "In Progress",
}

# Nhãn do sync quản lý: lọc trên Jira ra việc đang chờ người.
STATUS_LABEL: dict[TicketStatus, str] = {
    S.AWAITING_PLAN_APPROVAL: "cho-duyet-ke-hoach",
    S.AWAITING_MERGE_APPROVAL: "cho-duyet-merge",
    S.ESCALATED: "escalated",
    S.FAILED: "failed",
}
MANAGED_LABELS = frozenset(STATUS_LABEL.values())
SYNC_LABEL = "aico"  # issue do aico tạo; không phải nhãn intake nên không tự quay vòng thành ticket

# Trạng thái đáng ghi bình luận khi đổi (các bước giữa chừng chỉ đổi trạng thái).
STATUS_NOTE: dict[TicketStatus, str] = {
    S.AWAITING_PLAN_APPROVAL: "Kế hoạch xong, chờ người duyệt.",
    S.IN_PROGRESS: "Đang làm.",
    S.AWAITING_MERGE_APPROVAL: "Review/test đạt, chờ người duyệt merge.",
    S.ESCALATED: "Agent dừng, cần người xử lý.",
    S.DONE: "Đã merge, ticket xong.",
    S.FAILED: "Ticket thất bại.",
}

_TASK_CODE = re.compile(r"^(S(\d+)-T\d+)\b", re.IGNORECASE)
SPRINT_DOC = "docs/delivery/sprint-{:02d}.md"
MAX_SPRINTS = 50

# Đọc file trong repo (repo, đường dẫn) -> nội dung, None khi không có file.
ReadFile = Callable[[str, str], Awaitable[str | None]]


@dataclass(frozen=True)
class JiraIssue:
    key: str
    status: str
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class LabeledIssue:
    """Issue người giao cho aico qua nhãn intake."""

    key: str
    project_key: str
    summary: str
    description: str
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class SprintTask:
    code: str  # S02-T1
    sprint: int
    summary: str
    description: str


class JiraClient(Protocol):
    base_url: str

    async def find_by_ticket(self, project_key: str, ticket_id: str) -> JiraIssue | None: ...

    async def find_by_prefix(
        self, project_key: str, prefix: str, *, epic: bool = False
    ) -> str | None: ...

    async def get(self, key: str) -> JiraIssue: ...

    async def create(
        self, project_key: str, summary: str, description: str, *, parent: str | None
    ) -> JiraIssue: ...

    async def active_sprint(self, project_key: str) -> tuple[int, str] | None: ...

    async def add_to_sprint(self, sprint_id: int, key: str) -> None: ...

    async def transition(self, key: str, status: str) -> bool: ...

    async def set_labels(self, key: str, add: set[str], remove: set[str]) -> None: ...

    async def comment(self, key: str, text: str) -> None: ...

    async def link(self, key: str, url: str, title: str) -> None: ...

    async def list_labeled(self, project_keys: Iterable[str], label: str) -> list[LabeledIssue]: ...


def _adf(text: str) -> dict[str, Any]:
    """Văn bản thường -> Atlassian Document Format, mỗi dòng không rỗng một đoạn."""
    paragraphs = [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]}
        for line in text.splitlines()
        if line.strip()
    ]
    return {"type": "doc", "version": 1, "content": paragraphs}


_ADF_BLOCKS = frozenset({"paragraph", "heading", "listItem", "codeBlock", "blockquote", "rule"})


def adf_text(node: Any) -> str:
    """Atlassian Document Format -> văn bản thường (mỗi khối một dòng)."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return str(node.get("text", ""))
    if node.get("type") == "hardBreak":
        return "\n"
    inner = "".join(adf_text(child) for child in node.get("content") or [])
    if node.get("type") in _ADF_BLOCKS:
        return inner.strip("\n") + "\n"
    return inner


def _issue(raw: dict[str, Any]) -> JiraIssue:
    fields = raw.get("fields") or {}
    return JiraIssue(
        key=raw["key"],
        status=(fields.get("status") or {}).get("name", ""),
        labels=tuple(fields.get("labels") or ()),
    )


def _quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


class HttpJiraClient:
    """Jira Cloud REST v3 + Agile API, xác thực email + API token."""

    def __init__(
        self,
        base_url: str,
        email: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            auth=(email, token),
            headers={"Accept": "application/json"},
            timeout=30,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _json(self, method: str, path: str, **kw: Any) -> Any:
        resp = await self._http.request(method, path, **kw)
        if resp.status_code >= 400:
            raise RuntimeError(f"Jira {method} {path} lỗi {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else None

    async def _search(
        self,
        jql: str,
        *,
        fields: tuple[str, ...] = ("summary", "status", "labels"),
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        body = {"jql": jql, "fields": list(fields), "maxResults": limit}
        data = await self._json("POST", "/rest/api/3/search/jql", json=body)
        return list(data.get("issues") or [])

    async def find_by_ticket(self, project_key: str, ticket_id: str) -> JiraIssue | None:
        # Tìm theo cụm chính xác: issue tạo tay trước khi có sync cũng ghi mã ticket trong mô tả.
        issues = await self._search(
            f'project = "{project_key}" AND text ~ "\\"{_quote(ticket_id)}\\"" ORDER BY created ASC'
        )
        return _issue(issues[0]) if issues else None

    async def find_by_prefix(
        self, project_key: str, prefix: str, *, epic: bool = False
    ) -> str | None:
        # summary ~ là tìm mờ; lọc lại bằng tiền tố chính xác (S01 không khớp S010).
        kind = " AND issuetype = Epic" if epic else ""
        issues = await self._search(
            f'project = "{project_key}"{kind} AND summary ~ "\\"{_quote(prefix)}\\""'
            " ORDER BY created ASC"
        )
        pattern = re.compile(rf"^{re.escape(prefix)}\b", re.IGNORECASE)
        for raw in issues:
            if pattern.match(str((raw.get("fields") or {}).get("summary", ""))):
                return str(raw["key"])
        return None

    async def get(self, key: str) -> JiraIssue:
        return _issue(
            await self._json("GET", f"/rest/api/3/issue/{key}", params={"fields": "status,labels"})
        )

    async def create(
        self, project_key: str, summary: str, description: str, *, parent: str | None
    ) -> JiraIssue:
        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "issuetype": {"name": "Task"},
            "summary": summary[:250],
            "description": _adf(description),
            "labels": [SYNC_LABEL],
        }
        if parent:
            fields["parent"] = {"key": parent}
        data = await self._json("POST", "/rest/api/3/issue", json={"fields": fields})
        return await self.get(data["key"])

    async def active_sprint(self, project_key: str) -> tuple[int, str] | None:
        boards = await self._json(
            "GET", "/rest/agile/1.0/board", params={"projectKeyOrId": project_key}
        )
        for board in boards.get("values") or []:
            sprints = await self._json(
                "GET", f"/rest/agile/1.0/board/{board['id']}/sprint", params={"state": "active"}
            )
            for sprint in sprints.get("values") or []:
                return int(sprint["id"]), str(sprint["name"])
        return None

    async def add_to_sprint(self, sprint_id: int, key: str) -> None:
        await self._json(
            "POST", f"/rest/agile/1.0/sprint/{sprint_id}/issue", json={"issues": [key]}
        )

    async def transition(self, key: str, status: str) -> bool:
        data = await self._json("GET", f"/rest/api/3/issue/{key}/transitions")
        for t in data.get("transitions") or []:
            if str((t.get("to") or {}).get("name", "")).casefold() == status.casefold():
                await self._json(
                    "POST",
                    f"/rest/api/3/issue/{key}/transitions",
                    json={"transition": {"id": t["id"]}},
                )
                return True
        return False

    async def set_labels(self, key: str, add: set[str], remove: set[str]) -> None:
        ops = [{"add": x} for x in sorted(add)] + [{"remove": x} for x in sorted(remove)]
        if ops:
            await self._json("PUT", f"/rest/api/3/issue/{key}", json={"update": {"labels": ops}})

    async def comment(self, key: str, text: str) -> None:
        await self._json("POST", f"/rest/api/3/issue/{key}/comment", json={"body": _adf(text)})

    async def link(self, key: str, url: str, title: str) -> None:
        # globalId cố định theo URL: gọi lại chỉ cập nhật link cũ, không tạo trùng.
        body = {"globalId": url, "object": {"url": url, "title": title}}
        await self._json("POST", f"/rest/api/3/issue/{key}/remotelink", json=body)

    async def list_labeled(self, project_keys: Iterable[str], label: str) -> list[LabeledIssue]:
        keys = ", ".join(f'"{_quote(k)}"' for k in project_keys)
        issues = await self._search(
            f'project in ({keys}) AND labels = "{_quote(label)}" AND statusCategory != Done'
            " ORDER BY created ASC",
            fields=("summary", "description", "labels", "project"),
        )
        return [
            LabeledIssue(
                key=raw["key"],
                project_key=str(((raw.get("fields") or {}).get("project") or {}).get("key", "")),
                summary=str((raw.get("fields") or {}).get("summary", "")),
                description=adf_text((raw.get("fields") or {}).get("description")).strip(),
                labels=tuple((raw.get("fields") or {}).get("labels") or ()),
            )
            for raw in issues
        ]


def _sprint_number(text: str) -> int | None:
    match = re.search(r"(\d+)\s*$", text)
    return int(match.group(1)) if match else None


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _plain(text: str) -> str:
    return text.replace("**", "").replace("`", "").strip()


def parse_sprint_tasks(markdown: str, sprint: int) -> list[SprintTask]:
    """Task trong bảng Markdown có cột đầu là mã `S<n>-T<m>`; mã lặp ở bảng sau bỏ qua."""
    tasks: dict[str, SprintTask] = {}
    header: list[str] = []
    for line in markdown.splitlines():
        if not line.lstrip().startswith("|"):
            header = []
            continue
        cells = _cells(line)
        if all(re.fullmatch(r":?-{3,}:?", c) for c in cells if c):
            continue
        code = _TASK_CODE.match(cells[0])
        if code is None:
            header = cells  # dòng tiêu đề bảng
            continue
        task_code = code.group(1).upper()
        if task_code in tasks or len(cells) < 2:
            continue
        bold = re.match(r"\*\*(.+?)\*\*", cells[1])
        title = bold.group(1).rstrip(":").strip() if bold else _plain(cells[1])[:80]
        names = header if len(header) == len(cells) else [f"Cột {i + 1}" for i in range(len(cells))]
        details = [
            f"{name}: {_plain(value)}"
            for name, value in zip(names[1:], cells[1:], strict=True)
            if value
        ]
        tasks[task_code] = SprintTask(
            code=task_code,
            sprint=sprint,
            summary=f"{task_code}: {_plain(title)}",
            description="\n".join(details),
        )
    return list(tasks.values())


class JiraSync:
    """Một vòng polling: intake từ Jira, backlog từ tài liệu sprint, rồi đẩy ticket sang Jira."""

    def __init__(
        self,
        client: JiraClient,
        tickets: TicketStore,
        projects: Mapping[str, str],
        *,
        since: datetime | None = None,
        read_file: ReadFile | None = None,
        intake_label: str | None = None,
        budget_usd: float = 5.0,
        actor: str = "jira-sync",
    ) -> None:
        self._client = client
        self._tickets = tickets
        self._projects = dict(projects)  # repo -> project Jira
        self._repos = {key: repo for repo, key in self._projects.items()}
        self._since = since
        self._read_file = read_file
        self._intake_label = intake_label
        self._budget = budget_usd
        self._actor = actor
        # ticket_id -> (trạng thái, các URL MR) đã đẩy. Trống sau restart: đồng bộ lại,
        # không bình luận lặp.
        self._seen: dict[str, tuple[TicketStatus, tuple[str, ...]]] = {}
        # Repo cần đọc lại tài liệu sprint: tất cả lúc khởi động.
        self._backlog_due: set[str] = set(self._projects) if read_file is not None else set()

    async def sync_once(self) -> int:
        if self._intake_label:
            try:
                for t in await self.intake_once():
                    log.info("ticket mới %s từ %s", t.id, t.source_ref)
            except Exception:
                log.exception("intake Jira lỗi")
        for repo in sorted(self._backlog_due):
            try:
                created = await self.sync_backlog(repo)
            except Exception:
                log.exception("lập backlog Jira cho %s lỗi", repo)
                continue
            self._backlog_due.discard(repo)
            if created:
                log.info("backlog Jira %s: tạo %s", repo, ", ".join(created))
        return await self.sync_tickets()

    # --- Intake ---

    async def intake_once(self) -> list[Ticket]:
        assert self._intake_label
        tickets = await self._tickets.list()
        linked = {a.ref for t in tickets for a in t.artifacts if a.kind == "jira"}
        skip = {self._intake_label, SYNC_LABEL, *MANAGED_LABELS}
        created: list[Ticket] = []
        for issue in await self._client.list_labeled(self._repos, self._intake_label):
            repo = self._repos.get(issue.project_key)
            if repo is None or issue.key in linked:
                continue  # issue đã gắn với ticket (ví dụ do aico tạo rồi người thêm nhãn)
            ticket = Ticket(
                id=new_ticket_id(),
                title=issue.summary,
                body=issue.description,
                source=TicketSource.JIRA,
                source_ref=f"jira:{issue.key}",
                project=repo,
                labels=[lbl for lbl in issue.labels if lbl not in skip],
                budget_usd=self._budget,
            )
            saved, is_new = await self._tickets.create_from_source(ticket, actor=self._actor)
            await self._link(saved.id, issue.key)
            if is_new:
                created.append(saved)
        return created

    # --- Backlog từ tài liệu sprint ---

    async def sync_backlog(self, repo: str) -> list[str]:
        assert self._read_file is not None
        project_key = self._projects[repo]
        created: list[str] = []
        for n in range(1, MAX_SPRINTS + 1):
            content = await self._read_file(repo, SPRINT_DOC.format(n))
            if content is None:
                break
            for task in parse_sprint_tasks(content, n):
                if await self._client.find_by_prefix(project_key, task.code) is not None:
                    continue
                source = f"Nguồn: {repo} {SPRINT_DOC.format(n)}"
                issue = await self._create(
                    project_key, task.summary, f"{task.description}\n\n{source}"
                )
                created.append(issue.key)
        return created

    # --- Đồng bộ ticket -> issue ---

    def _relevant(self, ticket: Ticket) -> str | None:
        key = self._projects.get(ticket.project or "")
        if key is None:
            return None
        if self._since is not None and ticket.updated_at < self._since:
            return None
        return key

    async def sync_tickets(self) -> int:
        changed = 0
        for ticket in await self._tickets.list():
            project_key = self._relevant(ticket)
            if project_key is None:
                continue
            mrs = tuple(a.url or a.ref for a in ticket.artifacts if a.kind == "merge_request")
            state = (ticket.status, mrs)
            previous = self._seen.get(ticket.id)
            if previous == state:
                continue
            try:
                await self._sync_ticket(ticket, project_key, mrs, notify=previous is not None)
            except Exception:
                log.exception("đồng bộ Jira cho %s lỗi", ticket.id)
                continue
            self._seen[ticket.id] = state
            changed += 1
            if (
                previous is not None
                and previous[0] is not S.DONE
                and ticket.status is S.DONE
                and "docs" in ticket.labels
                and self._read_file is not None
                and ticket.project is not None
            ):
                # Ticket tài liệu vừa merge: có thể có sprint mới, vòng sau đọc lại.
                self._backlog_due.add(ticket.project)
        return changed

    async def _link(self, ticket_id: str, key: str) -> None:
        url = f"{self._client.base_url}/browse/{key}"
        await self._tickets.add_artifact(
            ticket_id, Artifact(kind="jira", ref=key, url=url), actor=self._actor
        )

    async def _issue_for(self, ticket: Ticket, project_key: str) -> JiraIssue:
        linked = next((a for a in ticket.artifacts if a.kind == "jira"), None)
        if linked is not None:
            return await self._client.get(linked.ref)
        issue = await self._client.find_by_ticket(project_key, ticket.id)
        code = _TASK_CODE.match(ticket.title)
        if issue is None and code is not None:
            # Task đã có issue từ backlog sprint: dùng lại, không tạo trùng.
            key = await self._client.find_by_prefix(project_key, code.group(1).upper())
            issue = await self._client.get(key) if key is not None else None
        if issue is None:
            description = (
                f"Ticket aico: {ticket.id}\nRepo: {ticket.project}\n\n{ticket.body[:4000]}"
            )
            issue = await self._create(project_key, ticket.title, description)
        await self._link(ticket.id, issue.key)
        return issue

    async def _create(self, project_key: str, summary: str, description: str) -> JiraIssue:
        code = _TASK_CODE.match(summary)
        parent = None
        if code is not None:
            parent = await self._client.find_by_prefix(
                project_key, f"S{int(code.group(2)):02d}", epic=True
            )
        issue = await self._client.create(project_key, summary, description, parent=parent)
        if code is not None:
            # Chỉ đưa vào sprint đang chạy khi đúng số sprint trong mã task (S02-T1 -> sprint 2).
            sprint = await self._client.active_sprint(project_key)
            if sprint is not None and _sprint_number(sprint[1]) == int(code.group(2)):
                await self._client.add_to_sprint(sprint[0], issue.key)
        log.info("tạo issue Jira %s: %s", issue.key, summary[:80])
        return issue

    async def _sync_ticket(
        self, ticket: Ticket, project_key: str, mrs: tuple[str, ...], *, notify: bool
    ) -> None:
        issue = await self._issue_for(ticket, project_key)
        target = JIRA_STATUS[ticket.status]
        moved = issue.status.casefold() == target.casefold() or await self._client.transition(
            issue.key, target
        )
        if not moved:
            log.warning("issue %s không có đường chuyển sang '%s'", issue.key, target)
        wanted = {STATUS_LABEL[ticket.status]} if ticket.status in STATUS_LABEL else set()
        current = set(issue.labels) & MANAGED_LABELS
        await self._client.set_labels(issue.key, wanted - current, current - wanted)
        for url in mrs:
            await self._client.link(issue.key, url, f"PR/MR {ticket.id}")
        if notify and ticket.status in STATUS_NOTE:
            await self._client.comment(issue.key, await self._note(ticket, mrs))

    async def _note(self, ticket: Ticket, mrs: tuple[str, ...]) -> str:
        lines = [f"aico {ticket.id}: {STATUS_NOTE[ticket.status]}"]
        if mrs:
            lines.append(f"PR: {mrs[-1]}")
        events = await self._tickets.events(ticket.id)
        last = next((e for e in reversed(events) if e.to_status == ticket.status), None)
        if last is not None:
            detail = last.data.get("reason") or last.data.get("review_notes")
            if detail:
                lines.append(str(detail)[:1500])
        return "\n".join(lines)

    async def run_forever(self, interval_seconds: float = 60) -> None:
        while True:
            try:
                changed = await self.sync_once()
                if changed:
                    log.info("đồng bộ Jira: %d ticket", changed)
            except Exception:
                log.exception("đồng bộ Jira lỗi")
            await asyncio.sleep(interval_seconds)
