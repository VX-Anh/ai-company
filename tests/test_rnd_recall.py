"""Recall@k của tìm kiếm tri thức: bộ câu hỏi chuẩn, cách tính, lệnh `aico recall`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from aico.db import create_pool
from aico.rnd import KnowledgeStore, parse_card, recall
from aico.rnd.cards import cards_from_dir
from aico.rnd.recall import Question, RecallError, parse_questions
from conftest import ROOT, reset_database
from test_knowledge import CACHING, METRIC, TRANSPORT, card_text


@pytest.fixture
async def kstore(pg_clean: str) -> AsyncIterator[KnowledgeStore]:
    pool = create_pool(pg_clean)
    await pool.open()
    try:
        store = KnowledgeStore(pool)
        texts = (CACHING, TRANSPORT, METRIC)
        cards = [parse_card(f"cards/x{i}.md", t) for i, t in enumerate(texts)]
        await store.rebuild(cards, commit="c1")
        yield store
    finally:
        await pool.close()


def test_shipped_questions_are_valid() -> None:
    """Bộ câu hỏi đi kèm repo: 20–30 câu (kế hoạch mục 8), id không trùng, id thẻ đúng dạng."""
    questions = recall.load_questions(ROOT / recall.DEFAULT_QUESTIONS)
    assert 20 <= len(questions) <= 30
    assert all(not e.startswith(("role/", "metric/")) for q in questions for e in q.expect)


def test_parse_questions_rejects_bad_input() -> None:
    ok = "questions:\n  - {id: a, query: prompt cache, expect: technique/prompt-caching}\n"
    assert parse_questions(ok) == [Question("a", "prompt cache", ("technique/prompt-caching",))]
    for bad in (
        "[]",
        "questions: []",
        "questions:\n  - {id: a, query: x}\n",
        "questions:\n  - {id: a, query: x, expect: [Prompt Caching]}\n",
        "questions:\n  - {id: a, query: x, expect: [t/a]}\n  - {id: a, query: y, expect: [t/b]}\n",
        "questions: [",
    ):
        with pytest.raises(RecallError):
            parse_questions(bad)
    with pytest.raises(RecallError):
        recall.load_questions(Path("khong-co-file.yaml"))


async def test_measure_counts_hits_misses_and_skips_unindexed(kstore: KnowledgeStore) -> None:
    questions = [
        Question("trung", "tiền tố prompt được cache", ("technique/prompt-caching",)),
        Question("truot", "streamable http thay sse", ("technique/prompt-caching",)),
        Question("nua", "API nhanh hơn", ("technique/prompt-caching", "technique/mcp-transport")),
        Question("chua-merge", "langgraph interrupt", ("tool/langgraph",)),
        # Thẻ chưa có trong chỉ mục bị bỏ khỏi mong đợi, phần còn lại vẫn đo.
        Question("mot-phan", "giao thức mcp", ("technique/mcp-transport", "tool/langgraph")),
    ]
    r = await recall.measure(kstore, questions)
    assert (r["mode"], r["k"], r["questions"], r["measured"]) == ("tool", 5, 5, 4)
    assert r["recall"] == round((1 + 0 + 0.5 + 1) / 4, 3)
    assert r["skipped"] == [{"id": "chua-merge", "missing": ["tool/langgraph"]}]
    assert [m["id"] for m in r["misses"]] == ["truot", "nua"]
    assert r["misses"][0]["got"] == ["technique/mcp-transport"]
    text = recall.format_report([r])
    assert "recall@5 = 0.625 trên 4/5 câu" in text and "trượt truot" in text
    assert "chua-merge" in text


async def test_inject_mode_uses_stricter_block_search(kstore: KnowledgeStore) -> None:
    """Một từ khoá khớp đủ cho tool search_knowledge nhưng không đủ cho khối <knowledge>."""
    q = [Question("mot-tu", "caching", ("technique/prompt-caching",))]
    assert (await recall.measure(kstore, q, mode="tool"))["recall"] == 1.0
    assert (await recall.measure(kstore, q, mode="inject"))["recall"] == 0.0
    # Thẻ gốc (metric/*) không bao giờ vào khối <knowledge> nên inject không tìm ra.
    anchor = [Question("goc", "chi phí token", ("metric/token-cost",))]
    assert (await recall.measure(kstore, anchor, mode="tool"))["recall"] == 1.0
    assert (await recall.measure(kstore, anchor, mode="inject"))["recall"] == 0.0


async def test_measure_rejects_bad_params_and_empty_index(kstore: KnowledgeStore) -> None:
    q = [Question("a", "prompt", ("technique/prompt-caching",))]
    for kwargs in ({"k": 0}, {"k": 9}, {"mode": "khac"}, {"mode": "inject", "k": 6}):
        with pytest.raises(RecallError):
            await recall.measure(kstore, q, **kwargs)  # type: ignore[arg-type]
    none = await recall.measure(kstore, [Question("x", "y z", ("tool/khong-co",))])
    assert none["recall"] is None and none["measured"] == 0
    assert "chưa đo được" in recall.format_report([none])


def test_cards_from_dir_reads_checkout(tmp_path: Path) -> None:
    (tmp_path / "cards" / "technique").mkdir(parents=True)
    (tmp_path / "cards" / "technique" / "prompt-caching.md").write_text(CACHING, "utf-8")
    (tmp_path / "cards" / "technique" / "trung.md").write_text(CACHING, "utf-8")
    (tmp_path / "cards" / "hong.md").write_text("không có front matter", "utf-8")
    (tmp_path / "README.md").write_text(card_text("technique/ngoai", "Ngoài cards", "x"), "utf-8")
    cards, skipped = cards_from_dir(tmp_path)
    assert [(c.id, c.card_path) for c in cards] == [
        ("technique/prompt-caching", "cards/technique/prompt-caching.md")
    ]
    assert len(skipped) == 2


def test_cli_recall_exit_code_follows_threshold(
    pg_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`aico recall` trên chỉ mục đang có: mã thoát 1 khi dưới ngưỡng hoặc bộ câu hỏi sai."""
    from aico.cli import _run, main  # noqa: PLC2701

    async def seed() -> None:
        await reset_database(pg_url)
        pool = create_pool(pg_url)
        await pool.open()
        try:
            cards = [parse_card("cards/a.md", CACHING), parse_card("cards/b.md", TRANSPORT)]
            await KnowledgeStore(pool).rebuild(cards, commit="c1")
        finally:
            await pool.close()

    _run(seed())
    monkeypatch.setenv("AICO_DATABASE_URL", pg_url)
    good = tmp_path / "q.yaml"
    good.write_text(
        "questions:\n"
        "  - {id: a, query: tiền tố prompt, expect: [technique/prompt-caching]}\n"
        "  - {id: b, query: stdio, expect: [technique/prompt-caching]}\n",
        "utf-8",
    )
    assert main(["recall", "--questions", str(good), "--min", "0.5"]) == 0
    assert "[tool] recall@5 = 0.500 trên 2/2 câu" in capsys.readouterr().out
    assert main(["recall", "--questions", str(good)]) == 1
    assert main(["recall", "--questions", str(tmp_path / "khong-co.yaml")]) == 1
    assert "LỖI" in capsys.readouterr().err
