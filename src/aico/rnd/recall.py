"""Recall@k của tìm kiếm tri thức trên bộ câu hỏi chuẩn (kế hoạch R&D mục 8, nghiệm thu GĐ2).

Bộ câu hỏi (`company/rnd/recall-questions.yaml`): mỗi câu ghi id thẻ phải tìm ra. Đo bằng đúng
hàm mà agent dùng, không gọi model:
- `tool`: `KnowledgeStore.search(query, k)` như tool `search_knowledge` gọi mặc định;
- `inject`: `KnowledgeStore.block_search(query)` như khối `<knowledge>` (chặt hơn: cần 2 từ khoá
  khớp ở id/tiêu đề/tag/tóm tắt). Câu hỏi không phải tiêu đề ticket nên số này chỉ để tham khảo.

Câu hỏi mà thẻ mong đợi chưa có trong chỉ mục (PR thẻ chưa merge) bị loại khỏi mẫu số và liệt kê
riêng, để số đo không phạt việc tìm kiếm vì thẻ chưa tồn tại.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from aico.rnd.store import BLOCK_CARDS, SEARCH_MAX_K, KnowledgeStore

DEFAULT_QUESTIONS = Path("company/rnd/recall-questions.yaml")
DEFAULT_K = 5
THRESHOLD = 0.8  # ngưỡng nghiệm thu GĐ2
MODES = ("tool", "inject")
_CARD_ID = re.compile(r"[a-z]+/[a-z0-9]+(-[a-z0-9]+)*")


class RecallError(ValueError):
    """Bộ câu hỏi sai định dạng."""


@dataclass(frozen=True)
class Question:
    id: str
    query: str
    expect: tuple[str, ...]


def parse_questions(text: str) -> list[Question]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RecallError(f"bộ câu hỏi không phải YAML hợp lệ ({exc})") from None
    items = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise RecallError("bộ câu hỏi cần khoá 'questions' là danh sách không rỗng")
    out: list[Question] = []
    seen: set[str] = set()
    for n, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise RecallError(f"câu {n}: phải là bảng khoá-giá trị")
        qid = str(item.get("id") or "").strip()
        query = str(item.get("query") or "").strip()
        expect = item.get("expect")
        if isinstance(expect, str):
            expect = [expect]
        if not qid or not query:
            raise RecallError(f"câu {n}: thiếu 'id' hoặc 'query'")
        if qid in seen:
            raise RecallError(f"câu {n}: trùng id '{qid}'")
        if not isinstance(expect, list) or not expect:
            raise RecallError(f"câu {qid}: 'expect' phải có ít nhất một id thẻ")
        ids = tuple(dict.fromkeys(str(e).strip() for e in expect))
        bad = [e for e in ids if not _CARD_ID.fullmatch(e)]
        if bad:
            raise RecallError(f"câu {qid}: id thẻ sai dạng loại/slug: {', '.join(bad)}")
        seen.add(qid)
        out.append(Question(qid, query, ids))
    return out


def load_questions(path: Path = DEFAULT_QUESTIONS) -> list[Question]:
    try:
        return parse_questions(path.read_text("utf-8"))
    except OSError as exc:
        raise RecallError(f"không đọc được bộ câu hỏi {path}: {exc}") from None


async def _hits(
    store: KnowledgeStore, query: str, mode: str, k: int, today: dt.date | None
) -> list[str]:
    if mode == "inject":
        rows = await store.block_search(query, today=today)
    else:
        rows = await store.search(query, k=k, today=today)
    return [str(r["id"]) for r in rows[:k]]


async def measure(
    store: KnowledgeStore,
    questions: Sequence[Question],
    *,
    k: int = DEFAULT_K,
    mode: str = "tool",
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Recall@k: trung bình trên các câu đo được của (số thẻ mong đợi nằm trong k kết quả đầu /
    số thẻ mong đợi có trong chỉ mục)."""
    if mode not in MODES:
        raise RecallError(f"mode phải là một trong: {', '.join(MODES)}")
    limit = BLOCK_CARDS if mode == "inject" else SEARCH_MAX_K
    if not 1 <= k <= limit:
        raise RecallError(f"k phải từ 1 tới {limit} với mode {mode}")
    indexed = {str(n["id"]) for n in await store.list_nodes() if not n["deprecated"]}
    skipped: list[dict[str, Any]] = []
    misses: list[dict[str, Any]] = []
    total = 0.0
    measured = 0
    for q in questions:
        want = [e for e in q.expect if e in indexed]
        absent = [e for e in q.expect if e not in indexed]
        if not want:
            skipped.append({"id": q.id, "missing": absent})
            continue
        got = await _hits(store, q.query, mode, k, today)
        found = [e for e in want if e in got]
        measured += 1
        total += len(found) / len(want)
        if len(found) < len(want):
            misses.append(
                {
                    "id": q.id,
                    "query": q.query,
                    "expect": want,
                    "found": found,
                    "got": got,
                }
            )
    return {
        "mode": mode,
        "k": k,
        "questions": len(questions),
        "measured": measured,
        "recall": round(total / measured, 3) if measured else None,
        "indexed_cards": len(indexed),
        "skipped": skipped,
        "misses": misses,
    }


def format_report(results: Sequence[dict[str, Any]], threshold: float = THRESHOLD) -> str:
    lines: list[str] = []
    for r in results:
        recall = "chưa đo được" if r["recall"] is None else f"{r['recall']:.3f}"
        lines.append(
            f"[{r['mode']}] recall@{r['k']} = {recall} trên {r['measured']}/{r['questions']} câu"
            f" ({r['indexed_cards']} thẻ trong chỉ mục; ngưỡng {threshold})"
        )
        for m in r["misses"]:
            lines.append(
                f"  trượt {m['id']}: mong {', '.join(m['expect'])};"
                f" được {', '.join(m['got']) or '(không có kết quả)'}"
            )
    if results and results[0]["skipped"]:
        ids = ", ".join(s["id"] for s in results[0]["skipped"])
        lines.append(f"không đo (thẻ mong đợi chưa có trong chỉ mục): {ids}")
    return "\n".join(lines)
