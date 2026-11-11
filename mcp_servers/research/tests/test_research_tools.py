"""Tool nghiên cứu: chặn địa chỉ nội bộ (cả qua chuyển hướng), đọc trang, kiểm trích dẫn, GitHub."""

from __future__ import annotations

import httpx
import pytest
from mcp.client.client import Client

from mcp_research.server import build_server
from mcp_research.tools import MAX_BYTES, ResearchError, ResearchTools, check_url, html_to_text

PUBLIC = {"docs.example.com": ["93.184.216.34"], "api.github.test": ["140.82.112.6"]}


async def resolve(host: str) -> list[str]:
    if host in PUBLIC:
        return PUBLIC[host]
    if host == "evil.example.com":  # tên công khai trỏ vào mạng nội bộ (DNS rebinding đơn giản)
        return ["10.0.0.5"]
    raise OSError("không có bản ghi")


PAGE = """<html><head><title>Docs  Caching</title><style>.x{}</style></head>
<body><script>alert(1)</script><h1>Prompt caching</h1>
<p>Cache prefixes are reused for up to 5&nbsp;minutes by default.</p></body></html>"""


def tools(routes: dict[str, httpx.Response]) -> ResearchTools:
    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url).split("?")[0]
        if key not in routes:
            return httpx.Response(404)
        return routes[key]

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ResearchTools(client, resolve, github_api="https://api.github.test")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://aico:8000/tickets",  # tên dịch vụ nội bộ: không phân giải được ở mạng riêng
        "http://localhost/",
        "https://evil.example.com/",
        "http://10.1.2.3/",
        "https://docs.example.com:8443/",
        "ftp://docs.example.com/",
        "https://user:pw@docs.example.com/",
    ],
)
async def test_internal_or_odd_urls_are_blocked(url: str) -> None:
    with pytest.raises(ResearchError):
        await check_url(url, resolve)


async def test_fetch_url_returns_text_without_scripts() -> None:
    t = tools({"https://docs.example.com/c": httpx.Response(200, html=PAGE)})
    page = await t.fetch_url("https://docs.example.com/c")
    assert page["title"] == "Docs Caching"
    assert "alert" not in page["text"] and ".x{}" not in page["text"]
    assert "reused for up to 5 minutes" in page["text"]
    assert len(page["sha256"]) == 64 and page["status"] == 200


async def test_redirect_to_internal_address_is_blocked() -> None:
    t = tools(
        {
            "https://docs.example.com/go": httpx.Response(
                302, headers={"location": "http://169.254.169.254/latest"}
            )
        }
    )
    with pytest.raises(ResearchError, match="nội bộ"):
        await t.fetch_url("https://docs.example.com/go")


async def test_binary_content_and_http_errors_are_reported() -> None:
    t = tools(
        {
            "https://docs.example.com/a.zip": httpx.Response(
                200, content=b"PK", headers={"content-type": "application/zip"}
            ),
            "https://docs.example.com/gone": httpx.Response(410),
        }
    )
    with pytest.raises(ResearchError, match="loại nội dung"):
        await t.fetch_url("https://docs.example.com/a.zip")
    with pytest.raises(ResearchError, match="410"):
        await t.fetch_url("https://docs.example.com/gone")


async def test_large_body_is_capped() -> None:
    big = "<html><body><p>" + "a" * (MAX_BYTES + 500) + "</p></body></html>"
    t = tools({"https://docs.example.com/big": httpx.Response(200, html=big)})
    page = await t.fetch_url("https://docs.example.com/big", max_chars=100)
    assert page["truncated"] is True and len(page["text"]) == 100


async def test_verify_quote_ignores_case_spacing_and_quote_style() -> None:
    t = tools({"https://docs.example.com/c": httpx.Response(200, html=PAGE)})
    hit = await t.verify_quote("https://docs.example.com/c", "cache  PREFIXES are reused")
    assert hit["found"] is True and "5 minutes" in hit["context"]
    miss = await t.verify_quote("https://docs.example.com/c", "cache lasts one hour always")
    assert miss["found"] is False and miss["context"] == ""
    with pytest.raises(ResearchError, match="quá ngắn"):
        await t.verify_quote("https://docs.example.com/c", "cache")


async def test_github_releases_and_search() -> None:
    releases = [
        {
            "tag_name": "v2.1.0",
            "name": "2.1",
            "published_at": "2026-11-01T00:00:00Z",
            "html_url": "https://github.com/o/r/releases/v2.1.0",
            "body": "x" * 5000,
        }
    ]
    search = {
        "items": [
            {
                "full_name": "o/agent",
                "stargazers_count": 900,
                "license": {"spdx_id": "MIT"},
                "html_url": "https://github.com/o/agent",
            }
        ]
    }
    t = tools(
        {
            "https://api.github.test/repos/o/r/releases": httpx.Response(200, json=releases),
            "https://api.github.test/search/repositories": httpx.Response(200, json=search),
        }
    )
    rel = await t.github_releases("o/r")
    assert rel["releases"][0]["tag"] == "v2.1.0" and len(rel["releases"][0]["body"]) == 4000
    found = await t.github_search_repos("coding agent", created_after="2026-10-01")
    assert found["query"] == "coding agent created:>2026-10-01"
    assert found["repos"][0]["license"] == "MIT"
    with pytest.raises(ResearchError):
        await t.github_releases("not a repo")
    with pytest.raises(ResearchError):
        await t.github_search_repos("x", created_after="hôm qua")


def test_html_to_text_keeps_blocks_on_separate_lines() -> None:
    title, text = html_to_text("<title>T</title><p>một</p><p>hai</p><noscript>z</noscript>")
    assert title == "T" and text == "một\nhai"


async def test_mcp_server_returns_blocked_url_as_tool_error() -> None:
    server = build_server(tools({}))
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert names == {"fetch_url", "verify_quote", "github_releases", "github_search_repos"}
        result = await client.call_tool("fetch_url", {"url": "http://127.0.0.1:8000/tickets"})
        assert result.is_error
