"""Đọc thẻ tri thức (Markdown + front matter YAML) thành dữ liệu cho chỉ mục.

Luật kiểm thẻ chỉ nằm ở `scripts/check_cards.py` của kho tri thức. Ở đây đọc phòng thủ: thẻ
thiếu trường bắt buộc thì bỏ qua và báo lý do, không làm hỏng cả lượt dựng chỉ mục.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import tarfile
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_FRONT = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)
_SECTION = re.compile(r"^## (.+?)\s*$", re.MULTILINE)
SUMMARY_SECTION = "Tóm tắt"
PRACTICE_SECTION = "Cách làm tốt nhất"


@dataclass(frozen=True)
class Source:
    local_id: str
    url: str
    kind: str
    trust: int
    published: dt.date | None
    retrieved: dt.date | None
    commit_sha: str | None
    quote: str | None


@dataclass(frozen=True)
class Edge:
    rel: str
    dst: str
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class Card:
    id: str
    kind: str
    title: str
    summary: str
    body_md: str
    tags: tuple[str, ...]
    confidence: int
    version: int
    deprecated: bool
    review_after: dt.date | None
    card_path: str
    edges: tuple[Edge, ...]
    sources: tuple[Source, ...]

    def section(self, name: str) -> str:
        return sections(self.body_md).get(name, "")

    @property
    def search_text(self) -> str:
        return fold(" ".join((self.id, self.title, " ".join(self.tags), self.body_md)))


class CardError(ValueError):
    """Thẻ không đọc được: thiếu front matter hoặc trường bắt buộc."""


def fold(text: str) -> str:
    """Bỏ dấu tiếng Việt, chữ thường: 'Đánh đổi' -> 'danh doi'. Dùng cho cả thẻ và câu truy vấn."""
    text = text.replace("đ", "d").replace("Đ", "D")
    plain = unicodedata.normalize("NFD", text)
    return "".join(c for c in plain if unicodedata.category(c) != "Mn").lower()


def sections(body: str) -> dict[str, str]:
    """Nội dung theo tiêu đề cấp 2 (## ...)."""
    found: dict[str, str] = {}
    matches = list(_SECTION.finditer(body))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        found[m.group(1).strip()] = body[m.end() : end].strip()
    return found


def _date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return dt.date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def parse_card(path: str, text: str) -> Card:
    m = _FRONT.match(text)
    if not m:
        raise CardError(f"{path}: thiếu front matter")
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError as exc:
        raise CardError(f"{path}: front matter không phải YAML hợp lệ ({exc})") from None
    if not isinstance(meta, dict):
        raise CardError(f"{path}: front matter không phải bảng khoá-giá trị")
    for key in ("id", "kind", "title"):
        if not str(meta.get(key) or "").strip():
            raise CardError(f"{path}: thiếu '{key}'")
    body = m.group(2).strip()
    try:
        confidence = min(3, max(1, int(meta.get("confidence") or 1)))
        version = int(meta.get("version") or 1)
    except (TypeError, ValueError):
        raise CardError(f"{path}: confidence/version không phải số") from None
    summary = " ".join(sections(body).get(SUMMARY_SECTION, "").split())
    edges = tuple(
        Edge(
            rel=str(r.get("rel", "")),
            dst=str(r.get("to", "")),
            source_ids=tuple(str(s) for s in (r.get("sources") or [])),
        )
        for r in (meta.get("relations") or [])
        if isinstance(r, dict) and r.get("rel") and r.get("to")
    )
    sources = tuple(
        Source(
            local_id=str(s.get("id")),
            url=str(s.get("url")),
            kind=str(s.get("kind") or "docs"),
            trust=min(3, max(1, int(s.get("trust") or 2))),
            published=_date(s.get("published")),
            retrieved=_date(s.get("retrieved")),
            commit_sha=str(s["commit"]) if s.get("commit") else None,
            quote=str(s["quote"]).replace("\x00", "") if s.get("quote") else None,
        )
        for s in (meta.get("sources") or [])
        if isinstance(s, dict) and s.get("id") and s.get("url")
    )
    tags = meta.get("tags") or []
    return Card(
        id=str(meta["id"]).strip(),
        kind=str(meta["kind"]).strip(),
        title=str(meta["title"]).strip(),
        summary=summary,
        body_md=body,
        tags=tuple(str(t) for t in tags) if isinstance(tags, list) else (),
        confidence=confidence,
        version=version,
        deprecated=bool(meta.get("deprecated")),
        review_after=_date(meta.get("review_after")),
        card_path=path,
        edges=edges,
        sources=sources,
    )


def _collect(files: Iterable[tuple[str, bytes]]) -> tuple[list[Card], list[str]]:
    cards: list[Card] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for path, raw in files:
        try:
            card = parse_card(path, raw.decode("utf-8"))
        except (CardError, UnicodeDecodeError, ValueError, TypeError) as exc:
            # Thẻ sai kiểu (trust: high, sources: 5...) chỉ bỏ thẻ đó, không làm hỏng chỉ mục.
            skipped.append(f"{exc}")
            continue
        if card.id in seen:
            skipped.append(f"{path}: trùng id '{card.id}'")
            continue
        seen.add(card.id)
        cards.append(card)
    return cards, skipped


def _archive_files(data: bytes) -> Iterator[tuple[str, bytes]]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            parts = member.name.split("/", 1)
            path = parts[1] if len(parts) == 2 else parts[0]
            if not member.isfile() or not path.startswith("cards/") or not path.endswith(".md"):
                continue
            handle = tar.extractfile(member)
            if handle is not None:
                yield path, handle.read()


def cards_from_archive(data: bytes) -> tuple[list[Card], list[str]]:
    """Thẻ trong tar.gz của kho (một thư mục gốc, như GitHub/GitLab trả): (thẻ, lỗi bỏ qua)."""
    return _collect(_archive_files(data))


def cards_from_dir(root: Path) -> tuple[list[Card], list[str]]:
    """Thẻ trong một bản checkout của kho tri thức (thư mục chứa `cards/`): (thẻ, lỗi bỏ qua).

    Chỉ dùng để đo ngoại tuyến (`aico recall --cards`); chỉ mục thật luôn dựng từ archive của main.
    """
    files = sorted((root / "cards").rglob("*.md"))
    return _collect((f.relative_to(root).as_posix(), f.read_bytes()) for f in files)
