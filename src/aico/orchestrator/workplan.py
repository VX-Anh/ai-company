"""Kế hoạch có cấu trúc: Tech Lead chia ticket thành các đầu việc chạy song song được.

Mỗi đầu việc có vai trò (coder, designer...), danh sách file được sửa (`owns`) và phụ thuộc.
Hai đầu việc có thể chạy cùng lúc thì `owns` không được chồng nhau; nếu chồng, aico tự xếp
chúng chạy nối tiếp. Agent chỉ được sửa file trong `owns`, nên các nhánh song song không
conflict. Kế hoạch và bản đồ kiến trúc (Mermaid) được ghi vào docs/ của repo.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
from typing import Any

ALL_FILES = "**"
MAX_ITEMS = 6
PLAN_DOC_DIR = "docs/plans"
ARCHITECTURE_DOC = "docs/architecture.md"

_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,15}$")
_FENCE = re.compile(r"```([A-Za-z0-9_-]*)[ \t]*\n(.*?)```", re.DOTALL)
_GLOB_CHARS = "*?["


@dataclass(frozen=True)
class WorkItem:
    id: str
    title: str
    role: str = "coder"
    owns: tuple[str, ...] = (ALL_FILES,)
    depends_on: tuple[str, ...] = ()
    acceptance: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "role": self.role,
            "owns": list(self.owns),
            "depends_on": list(self.depends_on),
            "acceptance": self.acceptance,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkItem:
        return cls(
            id=str(data["id"]),
            title=str(data.get("title", "")),
            role=str(data.get("role", "coder")),
            owns=tuple(data.get("owns") or (ALL_FILES,)),
            depends_on=tuple(data.get("depends_on") or ()),
            acceptance=str(data.get("acceptance", "")),
        )

    def allows(self, path: str) -> bool:
        return any(_match(path, pattern) for pattern in self.owns)


@dataclass(frozen=True)
class WorkPlan:
    items: tuple[WorkItem, ...]
    architecture: str | None = None  # sơ đồ Mermaid
    notes: tuple[str, ...] = field(default=())  # điều chỉnh aico đã làm khi đọc kế hoạch


def _normalize(pattern: str) -> str:
    pattern = pattern.strip().replace("\\", "/")
    while pattern.startswith("./"):
        pattern = pattern[2:]
    return pattern.lstrip("/") or ALL_FILES


def _match(path: str, pattern: str) -> bool:
    if pattern == ALL_FILES:
        return True
    if pattern.endswith("/"):  # thư mục
        return path.startswith(pattern)
    return path == pattern or fnmatchcase(path, pattern)


def _literal_prefix(pattern: str) -> str:
    cut = min((pattern.find(c) for c in _GLOB_CHARS if c in pattern), default=len(pattern))
    return pattern[:cut]


def owns_overlap(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """Hai vùng file có thể chồng nhau không (ước lượng thận trọng: nghi ngờ là chồng)."""
    for pa in a:
        for pb in b:
            if ALL_FILES in (pa, pb):
                return True
            la, lb = _literal_prefix(pa), _literal_prefix(pb)
            exact_a = la == pa and not pa.endswith("/")
            exact_b = lb == pb and not pb.endswith("/")
            if exact_a and exact_b:
                if pa == pb:
                    return True
                continue
            if exact_a and _match(pa, pb) or exact_b and _match(pb, pa):
                return True
            if not exact_a and not exact_b and (la.startswith(lb) or lb.startswith(la)):
                return True
    return False


def _ancestors(items: dict[str, WorkItem]) -> dict[str, set[str]]:
    memo: dict[str, set[str]] = {}

    def visit(item_id: str, stack: tuple[str, ...]) -> set[str]:
        if item_id in memo:
            return memo[item_id]
        if item_id in stack:
            raise ValueError(f"phụ thuộc vòng: {' -> '.join((*stack, item_id))}")
        found: set[str] = set()
        for dep in items[item_id].depends_on:
            found |= {dep} | visit(dep, (*stack, item_id))
        memo[item_id] = found
        return found

    for item_id in items:
        visit(item_id, ())
    return memo


def _json_blocks(text: str) -> list[Any]:
    found = []
    for lang, body in _FENCE.findall(text):
        if lang.lower() not in ("json", "aico-plan", ""):
            continue
        try:
            found.append(json.loads(body))
        except json.JSONDecodeError:
            continue
    return found


def parse_work_plan(text: str, allowed_roles: tuple[str, ...] = ("coder",)) -> WorkPlan:
    """Đọc khối ```json {"items": [...]}``` và ```mermaid``` cuối kế hoạch của Tech Lead.

    Không có danh sách đầu việc hợp lệ thì dùng một đầu việc coder sửa được mọi file
    (cách chạy cũ). Sửa các lỗi nhỏ và ghi lại trong notes thay vì từ chối kế hoạch.
    """
    notes: list[str] = []
    mermaid = [body.strip() for lang, body in _FENCE.findall(text) if lang.lower() == "mermaid"]
    architecture = mermaid[-1] if mermaid else None
    raw_items: list[Any] = []
    for block in reversed(_json_blocks(text)):
        if isinstance(block, dict) and isinstance(block.get("items"), list):
            raw_items = block["items"]
            break
    default_role = allowed_roles[0]
    items: dict[str, WorkItem] = {}
    for n, raw in enumerate(raw_items[:MAX_ITEMS], start=1):
        if not isinstance(raw, dict):
            continue
        item_id = str(raw.get("id") or f"W{n}")
        if not _ID.match(item_id) or item_id in items:
            notes.append(f"đổi id không hợp lệ/trùng '{item_id}' thành 'W{n}'")
            item_id = f"W{n}"
        role = str(raw.get("role") or default_role)
        if role not in allowed_roles:
            notes.append(f"{item_id}: vai trò '{role}' không dùng được, đổi thành {default_role}")
            role = default_role
        owns = tuple(_normalize(str(p)) for p in raw.get("owns") or [] if str(p).strip())
        if not owns:
            notes.append(f"{item_id}: thiếu owns, cho sửa mọi file")
            owns = (ALL_FILES,)
        items[item_id] = WorkItem(
            id=item_id,
            title=str(raw.get("title") or item_id),
            role=role,
            owns=owns,
            depends_on=tuple(str(d) for d in raw.get("depends_on") or []),
            acceptance=str(raw.get("acceptance") or ""),
        )
    if len(raw_items) > MAX_ITEMS:
        notes.append(f"chỉ giữ {MAX_ITEMS} đầu việc đầu tiên")
    if not items:
        title = "Thực hiện kế hoạch"
        return WorkPlan((WorkItem("W1", title, default_role),), architecture, tuple(notes))

    # Bỏ phụ thuộc tới đầu việc không tồn tại; có vòng thì chạy nối tiếp theo thứ tự.
    for item_id, item in list(items.items()):
        unknown = [d for d in item.depends_on if d not in items or d == item_id]
        if unknown:
            notes.append(f"{item_id}: bỏ phụ thuộc không tồn tại {unknown}")
            kept = tuple(d for d in item.depends_on if d not in unknown)
            items[item_id] = _replace(item, depends_on=kept)
    try:
        ancestors = _ancestors(items)
    except ValueError as exc:
        notes.append(f"{exc}; chạy nối tiếp theo thứ tự")
        ordered = list(items)
        for n, cur in enumerate(ordered):
            items[cur] = _replace(items[cur], depends_on=(ordered[n - 1],) if n else ())
        ancestors = _ancestors(items)

    # Hai đầu việc chạy cùng lúc mà vùng file chồng nhau: cho đầu việc sau chờ đầu việc trước.
    ordered = list(items)
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            related = a in ancestors[b] or b in ancestors[a]
            if not related and owns_overlap(items[a].owns, items[b].owns):
                notes.append(f"{b} và {a} sửa chung file, cho {b} chạy sau {a}")
                items[b] = _replace(items[b], depends_on=(*items[b].depends_on, a))
                ancestors = _ancestors(items)
    return WorkPlan(tuple(items.values()), architecture, tuple(notes))


def _replace(item: WorkItem, **changes: Any) -> WorkItem:
    return replace(item, **changes)


def fix_item(feedback: str) -> WorkItem:
    """Vòng sửa sau review: một đầu việc coder sửa được mọi file, dựa trên góp ý."""
    title = feedback.strip().splitlines()[0][:80] if feedback.strip() else "Sửa theo góp ý"
    return WorkItem("FIX", f"Sửa theo góp ý: {title}", "coder", (ALL_FILES,))


def plan_doc_path(ticket_id: str) -> str:
    return f"{PLAN_DOC_DIR}/{ticket_id}.md"


def render_plan_doc(
    ticket_id: str,
    title: str,
    plan: str,
    items: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    attempt: int,
    notes: list[str],
) -> str:
    """Tài liệu kế hoạch lưu trong repo để mọi agent nắm phần đã làm."""
    by_id = {r["id"]: r for r in results}
    lines = [
        f"# {ticket_id}: {title}",
        "",
        f"Lần code: {attempt}. File do aico tạo, cập nhật mỗi lần code.",
        "",
        "## Đầu việc",
        "",
        "| Id | Việc | Vai trò | Được sửa | Chờ | Kết quả |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in items:
        r = by_id.get(item["id"], {})
        status = r.get("status", "chưa chạy")
        if r.get("worker"):
            status += f" ({r['worker']})"
        lines.append(
            f"| {item['id']} | {item['title']} | {item['role']} | "
            f"{', '.join(f'`{p}`' for p in item['owns'])} | "
            f"{', '.join(item['depends_on']) or '-'} | {status} |"
        )
    extra = [r for r in results if r["id"] not in {i["id"] for i in items}]
    for r in extra:
        lines.append(f"| {r['id']} | {r.get('title', '')} | coder | `**` | - | {r['status']} |")
    for r in results:
        if r.get("rejected"):
            lines.append("")
            lines.append(f"- {r['id']}: bỏ thay đổi ngoài phạm vi: {', '.join(r['rejected'])}")
    if notes:
        lines += ["", "## Điều chỉnh của aico", "", *[f"- {n}" for n in notes]]
    lines += ["", "## Kế hoạch", "", plan.strip(), ""]
    return "\n".join(lines)


def render_architecture_doc(mermaid: str, ticket_id: str) -> str:
    return "\n".join(
        [
            "# Kiến trúc",
            "",
            f"Bản đồ do Tech Lead cập nhật (lần gần nhất: {ticket_id}). Agent đọc file này",
            "trước khi làm để biết các phần đã có; sửa kiến trúc thì cập nhật sơ đồ.",
            "",
            "```mermaid",
            mermaid.strip(),
            "```",
            "",
        ]
    )
