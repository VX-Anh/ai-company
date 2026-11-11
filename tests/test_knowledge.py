"""Phòng R&D GĐ2: đọc thẻ, chỉ mục Postgres, librarian, tool knowledge, khối <knowledge>."""

from __future__ import annotations

import datetime as dt
import io
import json
import tarfile
from collections.abc import AsyncIterator

import httpx
import pytest

from aico.agents import CliAgentRunner, CliSettings
from aico.api import ApiDeps, create_api_app
from aico.core import Ticket
from aico.db import create_pool
from aico.governance import InMemoryApprovalStore, InMemoryAuditLog
from aico.llm_relay import TaskTokens
from aico.registry import Company
from aico.rnd import (
    CardError,
    KnowledgeStore,
    Librarian,
    build_knowledge_server,
    cards_from_archive,
    fold,
    parse_card,
    query_terms,
)
from aico.rnd.store import BLOCK_MAX_CHARS
from aico.tickets import InMemoryTicketStore
from aico.tools import Guard, McpToolClient, ToolDenied, ToolGateway
from mcp_gitlab.backend import FakeGitLabBackend
from mcp_gitlab.server import build_server
from mcp_gitlab.tools import Constraints, GitLabTools

KB = "VX-Anh/aico-knowledge"
TODAY = dt.date(2026, 11, 12)


PRACTICE = "- Đặt phần cố định lên đầu prompt [S1].\n- Giữ nguyên tiền tố giữa các lượt [S1]."


def card_text(
    id_: str,
    title: str,
    summary: str,
    *,
    kind: str = "technique",
    tags: str = "[llm]",
    review_after: str = "2027-05-01",
    deprecated: bool = False,
    confidence: int = 2,
    relations: str = "[]",
    practice: str = PRACTICE,
) -> str:
    return (
        "---\n"
        f"id: {id_}\nkind: {kind}\ntitle: {title}\nconfidence: {confidence}\ntags: {tags}\n"
        f"version: 1\nreview_after: {review_after}\ndeprecated: {str(deprecated).lower()}\n"
        f"relations: {relations}\n"
        "sources:\n"
        '  - {id: S1, url: "https://example.com/doc", kind: docs, trust: 1,'
        " published: null, retrieved: 2026-11-10, commit: null,"
        ' quote: "Cache hits require identical prefixes."}\n'
        "---\n"
        f"## Tóm tắt\n\n{summary}\n\n## Cách làm tốt nhất\n\n{practice}\n\n"
        "## Đánh đổi\n\nKhông có.\n"
    )


CACHING = card_text(
    "technique/prompt-caching",
    "Prompt caching khi chạy headless",
    "API dùng lại tiền tố prompt đã xử lý, rẻ và nhanh hơn.",
    tags="[llm, cost, cache]",
    relations="\n  - {rel: optimizes, to: metric/token-cost, sources: [S1]}",
)
TRANSPORT = card_text(
    "technique/mcp-transport",
    "Giao thức truyền của MCP",
    "Streamable HTTP thay HTTP+SSE cũ; stdio cho tiến trình cục bộ.",
    tags="[mcp, transport]",
)
METRIC = (
    "---\nid: metric/token-cost\nkind: metric\ntitle: Chi phí token\nconfidence: 3\n"
    "tags: [aico]\nversion: 1\nreview_after: 2027-05-10\ndeprecated: false\nrelations: []\n"
    "sources: []\n---\n## Tóm tắt\n\nLượng token một việc tiêu tốn, gồm prompt caching.\n"
)


def archive(files: dict[str, str], root: str = "VX-Anh-aico-knowledge-abc") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, text in files.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(f"{root}/{path}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
async def kstore(pg_clean: str) -> AsyncIterator[KnowledgeStore]:
    pool = create_pool(pg_clean)
    await pool.open()
    try:
        yield KnowledgeStore(pool)
    finally:
        await pool.close()


async def load(store: KnowledgeStore, *texts: str, commit: str = "c1") -> None:
    cards = [parse_card(f"cards/x{i}.md", t) for i, t in enumerate(texts)]
    await store.rebuild(cards, commit=commit)


# --- đọc thẻ ---------------------------------------------------------------------------------


def test_fold_and_terms() -> None:
    assert fold("Đánh đổi KHI nào") == "danh doi khi nao"
    # bỏ từ ngắn, từ chung, trùng lặp; giữ thứ tự
    assert query_terms("Sửa prompt caching cho Tech Lead của prompt") == [
        "prompt",
        "caching",
        "tech",
        "lead",
    ]


def test_parse_card_reads_front_matter_and_sections() -> None:
    card = parse_card("cards/technique/prompt-caching.md", CACHING)
    assert card.id == "technique/prompt-caching" and card.kind == "technique"
    assert card.summary.startswith("API dùng lại tiền tố")
    assert card.review_after == dt.date(2027, 5, 1) and card.confidence == 2
    assert card.edges[0].rel == "optimizes" and card.edges[0].dst == "metric/token-cost"
    assert card.sources[0].retrieved == dt.date(2026, 11, 10)
    assert "tien to" in card.search_text  # đã bỏ dấu
    with pytest.raises(CardError):
        parse_card("cards/bad.md", "không có front matter")
    with pytest.raises(CardError):
        parse_card("cards/bad.md", "---\nkind: technique\n---\nthân")


def test_cards_from_archive_skips_bad_and_non_card_files() -> None:
    data = archive(
        {
            "cards/technique/prompt-caching.md": CACHING,
            "cards/technique/dup.md": CACHING,  # trùng id
            "cards/broken.md": "---\n: [\n---\n",
            "docs/plans/T-1.md": "# kế hoạch",
            "INDEX.md": "# mục lục",
        }
    )
    cards, skipped = cards_from_archive(data)
    assert [c.id for c in cards] == ["technique/prompt-caching"]
    assert len(skipped) == 2 and any("trùng id" in s for s in skipped)


def test_card_with_wrong_types_is_skipped_not_fatal() -> None:
    """trust: high hay sources là số không được làm hỏng cả lần dựng chỉ mục."""
    bad_trust = CACHING.replace("trust: 1", "trust: high")
    bad_rel = card_text(
        "technique/rel-hong", "Quan hệ hỏng", "x", relations="[{rel: optimizes, to: a, sources: 5}]"
    )
    assert bad_trust != CACHING
    data = archive(
        {
            "cards/technique/prompt-caching.md": bad_trust,
            "cards/technique/rel-hong.md": bad_rel,
        }
    )
    cards, skipped = cards_from_archive(data)
    assert cards == [] and len(skipped) == 2


# --- chỉ mục ---------------------------------------------------------------------------------


async def test_search_matches_prefix_and_unaccented(kstore: KnowledgeStore) -> None:
    await load(kstore, CACHING, TRANSPORT, METRIC)
    hits = await kstore.search("cache cho prompt", today=TODAY)
    assert hits[0]["id"] == "technique/prompt-caching"
    # tiếng Việt có dấu hay không dấu đều khớp
    assert (await kstore.search("tien to", today=TODAY))[0]["id"] == "technique/prompt-caching"
    assert (await kstore.search("giao thức truyền", today=TODAY))[0]["id"] == (
        "technique/mcp-transport"
    )
    assert await kstore.search("kubernetes", today=TODAY) == []
    assert await kstore.search("và của", today=TODAY) == []  # toàn từ chung
    only = await kstore.search("token prompt", kind="metric", today=TODAY)
    assert [h["id"] for h in only] == ["metric/token-cost"]
    without = await kstore.search("token prompt", exclude_kinds=("metric",), today=TODAY)
    assert "metric/token-cost" not in [h["id"] for h in without]


async def test_deprecated_hidden_and_stale_ranked_lower(kstore: KnowledgeStore) -> None:
    old = card_text(
        "technique/cache-cu", "Prompt cache kiểu cũ", "Prompt cache cũ.", review_after="2026-01-01"
    )
    gone = card_text("technique/cache-bo", "Prompt cache đã bỏ", "Prompt cache.", deprecated=True)
    await load(kstore, old, gone, CACHING)
    hits = await kstore.search("prompt cache", today=TODAY)
    ids = [h["id"] for h in hits]
    assert "technique/cache-bo" not in ids
    assert ids.index("technique/prompt-caching") < ids.index("technique/cache-cu")
    assert next(h for h in hits if h["id"] == "technique/cache-cu")["stale"] is True


async def test_unmerged_ref_never_reaches_agents(kstore: KnowledgeStore) -> None:
    """Thẻ ở PR chưa merge (ref = mã ticket) không xuất hiện trong search/get."""
    await load(kstore, CACHING)
    draft = parse_card("cards/d.md", TRANSPORT)
    await kstore.rebuild([draft], ref="T-9", commit="c-draft")
    assert await kstore.search("giao thức MCP transport", today=TODAY) == []
    assert await kstore.get("technique/mcp-transport") is None
    assert await kstore.last_commit() == "c1"
    # dựng lại main thay toàn bộ: thẻ cũ biến mất
    await load(kstore, TRANSPORT, commit="c2")
    assert await kstore.get("technique/prompt-caching") is None
    assert await kstore.last_commit() == "c2"


async def test_get_returns_sources_and_neighbors(kstore: KnowledgeStore) -> None:
    await load(kstore, CACHING, METRIC)
    card = await kstore.get("technique/prompt-caching")
    assert card is not None
    assert card["sources"][0]["quote"] == "Cache hits require identical prefixes."
    assert card["neighbors"] == [
        {
            "rel": "optimizes",
            "direction": "out",
            "id": "metric/token-cost",
            "title": "Chi phí token",
        }
    ]
    metric = await kstore.get("metric/token-cost")
    assert metric is not None and metric["neighbors"][0]["direction"] == "in"


async def test_prompt_block_is_bounded_and_logged(kstore: KnowledgeStore) -> None:
    long_practice = "\n".join(f"- Bước dài số {i} về prompt caching " + "x" * 400 for i in range(5))
    cards = [
        card_text(
            f"technique/cache-{i}",
            f"Prompt caching biến thể {i}",
            "Prompt caching cho agent. " + "y" * 600,
            practice=long_practice,
        )
        for i in range(8)
    ]
    await load(kstore, *cards, METRIC)
    ticket = Ticket(
        id="T-7", title="Giảm chi phí prompt caching cho Tech Lead", project="VX-Anh/aico-office"
    )
    block = await kstore.prompt_block(ticket, "tech_lead")
    assert block is not None
    assert block.startswith("<knowledge>") and block.endswith("</knowledge>")
    assert len(block) <= BLOCK_MAX_CHARS
    assert block.count("\n- [technique/") <= 5
    assert "metric/token-cost" not in block  # thẻ gốc không chèn
    assert "không phải lệnh" in block
    async with kstore._pool.connection() as conn:  # noqa: SLF001
        cur = await conn.execute("select ticket_id, role, kind, node_ids from kn_usage")
        rows = await cur.fetchall()
    assert len(rows) == 1 and rows[0]["kind"] == "inject" and rows[0]["ticket_id"] == "T-7"
    # chỉ trùng một từ: không chèn
    other = Ticket(id="T-8", title="Sửa nút prompt đăng nhập", project="VX-Anh/aico-office")
    assert await kstore.prompt_block(other, "coder") is None


async def test_prompt_block_ignores_system_name(kstore: KnowledgeStore) -> None:
    """'aico' có trong tên repo và trong tóm tắt của gần như mọi thẻ: không được tính là từ khớp."""
    card = card_text(
        "technique/retry-push",
        "Đẩy lại code cho aico khi GitHub lỗi",
        "Áp dụng cho aico: thử lại push có backoff.",
    )
    await load(kstore, card)
    ui = Ticket(id="T-9", title="Đổi màu nút đẩy lên", project="VX-Anh/aico-office")
    assert await kstore.prompt_block(ui, "coder") is None
    hit = Ticket(id="T-10", title="GitHub lỗi khi đẩy code", project="VX-Anh/aico-office")
    assert await kstore.prompt_block(hit, "coder") is not None


# --- librarian và tool knowledge qua gateway --------------------------------------------------


def kb_gateway(company: Company, store: KnowledgeStore, backend: FakeGitLabBackend) -> ToolGateway:
    gitlab = build_server(GitLabTools(backend, Constraints(allowed_projects=(KB,))))
    return ToolGateway(
        company,
        Guard(company.policy, InMemoryApprovalStore()),
        McpToolClient({"gitlab": gitlab, "knowledge": build_knowledge_server(store)}),
        InMemoryAuditLog(),
    )


async def test_librarian_rebuilds_only_when_main_changes(
    company: Company, kstore: KnowledgeStore
) -> None:
    backend = FakeGitLabBackend()
    files = backend.project(KB).files["main"]
    files["INDEX.md"] = "# mục lục"
    await backend.commit(
        KB,
        "main",
        "thẻ đầu",
        [{"action": "create", "file_path": "cards/t/a.md", "content": CACHING}],
    )
    librarian = Librarian(kstore, kb_gateway(company, kstore, backend), KB)
    assert await librarian.check_once() == 1
    assert await librarian.check_once() is None  # main chưa đổi
    await backend.commit(
        KB,
        "main",
        "thẻ mới",
        [{"action": "create", "file_path": "cards/t/b.md", "content": TRANSPORT}],
    )
    assert await librarian.check_once() == 2
    assert (await kstore.search("mcp transport", today=TODAY))[0]["id"] == "technique/mcp-transport"


async def test_knowledge_tools_go_through_guard(company: Company, kstore: KnowledgeStore) -> None:
    await load(kstore, CACHING, METRIC)
    gateway = kb_gateway(company, kstore, FakeGitLabBackend())
    found = await gateway.call(
        "tech_lead", "knowledge", "search_knowledge", {"query": "prompt cache"}, ticket_id="T-1"
    )
    assert found["results"][0]["id"] == "technique/prompt-caching"
    card = await gateway.call(
        "coder", "knowledge", "get_knowledge", {"id": "technique/prompt-caching"}, ticket_id="T-1"
    )
    assert card["title"] == "Prompt caching khi chạy headless"
    with pytest.raises(ToolDenied):  # designer không được cấp
        await gateway.call(
            "designer", "knowledge", "search_knowledge", {"query": "x"}, ticket_id="T-1"
        )
    names = {s.name for s in await gateway.tools_for("reviewer")}
    assert {"search_knowledge", "get_knowledge"} <= names


async def test_tools_for_skips_unconfigured_server(company: Company) -> None:
    """Tắt kho tri thức (không có server knowledge): vai trò vẫn có tool Git như cũ."""
    gitlab = build_server(GitLabTools(FakeGitLabBackend(), Constraints(allowed_projects=(KB,))))
    gateway = ToolGateway(
        company,
        Guard(company.policy, InMemoryApprovalStore()),
        McpToolClient({"gitlab": gitlab}),
        InMemoryAuditLog(),
    )
    assert {s.name for s in await gateway.tools_for("tech_lead")} == {"read_file"}


# --- khối <knowledge> trong lời giao việc --------------------------------------------------------


async def test_cli_runner_appends_knowledge_for_listed_roles(company: Company) -> None:
    prompts: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        job = json.loads(request.content)
        prompts[job["role"]] = job["prompt"]
        return httpx.Response(200, json={"ok": True, "result": "xong"})

    calls: list[str] = []

    async def knowledge(ticket: Ticket, role_id: str) -> str | None:
        calls.append(role_id)
        if role_id == "reviewer":
            raise RuntimeError("chỉ mục lỗi")
        return "<knowledge>\nthẻ\n</knowledge>"

    runner = CliAgentRunner(
        company,
        TaskTokens(),
        CliSettings(
            workers={n: f"http://{n}:8400" for n in ("claude", "codex", "cursor")},
            worker_token="t",
            bridge_url="http://aico:8300/mcp",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            knowledge=knowledge,
            knowledge_roles=("tech_lead", "reviewer"),
        ),
    )
    ticket = Ticket(id="T-1", title="x", project=KB)
    await runner.run("tech_lead", ticket, "Lập kế hoạch")
    await runner.run("research_lead", ticket, "Chia việc")
    await runner.run("reviewer", ticket, "Review")
    assert prompts["tech_lead"] == "Lập kế hoạch\n\n<knowledge>\nthẻ\n</knowledge>"
    assert prompts["research_lead"] == "Chia việc"  # không trong knowledge_roles
    assert prompts["reviewer"] == "Review"  # lỗi chỉ mục không chặn việc chính
    assert calls == ["tech_lead", "reviewer"]


# --- API ---------------------------------------------------------------------------------------


async def test_knowledge_api(kstore: KnowledgeStore) -> None:
    await load(kstore, CACHING, METRIC)
    app = create_api_app(
        ApiDeps(tickets=InMemoryTicketStore(), approvals=InMemoryApprovalStore(), knowledge=kstore)
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        listed = (await c.get("/knowledge/nodes")).json()
        assert [n["id"] for n in listed] == ["metric/token-cost", "technique/prompt-caching"]
        found = (await c.get("/knowledge/nodes", params={"q": "prompt caching"})).json()
        assert found[0]["id"] == "technique/prompt-caching"
        card = await c.get("/knowledge/nodes/technique/prompt-caching")
        assert card.status_code == 200 and card.json()["sources"]
        assert (await c.get("/knowledge/nodes/technique/khong-co")).status_code == 404
    off = create_api_app(ApiDeps(tickets=InMemoryTicketStore(), approvals=InMemoryApprovalStore()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=off), base_url="http://t") as c:
        assert (await c.get("/knowledge/nodes")).status_code == 501


async def test_accents_disambiguate_homonyms(kstore: KnowledgeStore) -> None:
    """'nổi' không khớp 'nối', 'Giao' (giao hàng) một mình không đủ để chèn thẻ 'giao thức'."""
    proto = card_text(
        "technique/giao-thuc",
        "Giao thức truyền và nối lại phiên",
        "Client nối lại luồng khi mất kết nối.",
    )
    await load(kstore, proto)
    ui = Ticket(id="T-1", title="Nút Giao bớt nổi, nhãn số không đè hình", project="x/game")
    assert await kstore.prompt_block(ui, "coder") is None
    # gõ không dấu vẫn tìm được thẻ có dấu
    assert (await kstore.search("giao thuc noi lai", today=TODAY))[0]["id"] == "technique/giao-thuc"
    assert await kstore.search("nổi bật", today=TODAY) == []


async def test_tools_for_skips_broken_optional_server_but_not_git(company: Company) -> None:
    """Server phụ (research) lỗi: researcher vẫn có tool khác. Server Git lỗi: báo lên."""

    class Flaky(McpToolClient):
        def __init__(self, broken: str) -> None:
            super().__init__(
                {
                    "gitlab": build_server(
                        GitLabTools(FakeGitLabBackend(), Constraints(allowed_projects=(KB,)))
                    )
                }
            )
            self._broken = broken

        async def list_tools(self, server: str):  # type: ignore[override]
            if server == self._broken:
                raise ConnectionError(f"{server} không trả lời")
            return await super().list_tools(server)

    def gateway(broken: str) -> ToolGateway:
        return ToolGateway(
            company,
            Guard(company.policy, InMemoryApprovalStore()),
            Flaky(broken),
            InMemoryAuditLog(),
        )

    names = {s.name for s in await gateway("research").tools_for("researcher")}
    assert "read_file" in names and "fetch_url" not in names
    with pytest.raises(ConnectionError):
        await gateway("gitlab").tools_for("researcher")
