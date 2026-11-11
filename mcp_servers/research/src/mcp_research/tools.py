"""Tool nghiên cứu chỉ đọc cho Phòng R&D: đọc trang web công khai, kiểm trích dẫn, đọc GitHub.

An toàn:
- Chỉ http/https, cổng 80/443; mọi địa chỉ IP mà tên miền trỏ tới phải là địa chỉ công khai
  (`is_global`), kiểm lại ở từng bước chuyển hướng: chặn gọi vào aico, Postgres, metadata cloud.
  Lớp thứ hai là mạng Docker riêng của container (không thấy mạng core).
- Giới hạn cỡ tải về, thời gian, số lần chuyển hướng. Nội dung trả về là dữ liệu, không phải lệnh.
- Không giữ secret: GitHub gọi không token (giới hạn 60 lượt/giờ là đủ cho scout).
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

MAX_BYTES = 2_000_000
MAX_REDIRECTS = 5
DEFAULT_CHARS = 20_000
MAX_CHARS = 60_000
TOTAL_SECONDS = 45
ALLOWED_PORTS = frozenset({80, 443})
GITHUB_API = "https://api.github.com"
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

# host -> danh sách IP (chuỗi). Test thay bằng hàm giả.
Resolver = Callable[[str], Awaitable[list[str]]]


class ResearchError(ValueError):
    """Lỗi đã lường trước (URL bị chặn, trang lỗi...): trả nguyên văn cho agent đọc."""


async def system_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def _now() -> str:
    return datetime.now(UTC).date().isoformat()


async def check_url(url: str, resolve: Resolver) -> str:
    """Trả IP công khai đã kiểm để kết nối thẳng tới đó (ghim IP, chống DNS rebinding: phân giải
    lần hai lúc kết nối có thể ra IP nội bộ); ném ResearchError nếu URL không được phép."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ResearchError(f"chỉ đọc http/https: {url}")
    host = parts.hostname
    if not host:
        raise ResearchError(f"URL thiếu tên máy: {url}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise ResearchError(f"chỉ cho cổng 80/443: {url}")
    if parts.username or parts.password:
        raise ResearchError("URL không được chứa thông tin đăng nhập")
    try:
        literal = ipaddress.ip_address(host)
        addresses = [str(literal)]
    except ValueError:
        if host.lower() in ("localhost",) or host.lower().endswith((".local", ".internal")):
            raise ResearchError(f"tên máy nội bộ bị chặn: {host}") from None
        try:
            addresses = await resolve(host)
        except OSError as exc:
            raise ResearchError(f"không phân giải được {host}: {exc}") from exc
    if not addresses:
        raise ResearchError(f"không phân giải được {host}")
    for addr in addresses:
        ip = ipaddress.ip_address(addr)
        # ::a.b.c.d (IPv4-compatible) và NAT64 64:ff9b::/96 được Python coi là global nhưng có
        # thể trỏ về IPv4 nội bộ: xét phần IPv4 bên trong.
        inner = None
        if isinstance(ip, ipaddress.IPv6Address) and (
            ip in ipaddress.IPv6Network("64:ff9b::/96") or (int(ip) >> 32 == 0 and int(ip) > 0xFFFF)
        ):
            inner = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if not ip.is_global or (inner is not None and not inner.is_global):
            raise ResearchError(f"địa chỉ nội bộ bị chặn: {host} -> {addr}")
    return addresses[0]


def pinned_url(url: str, ip: str) -> str:
    """URL trỏ thẳng tới IP đã kiểm, giữ scheme, cổng, đường dẫn (Host/SNI gửi riêng)."""
    parts = urlsplit(url)
    host = f"[{ip}]" if ":" in ip else ip
    netloc = f"{host}:{parts.port}" if parts.port else host
    return parts._replace(netloc=netloc).geturl()


class _Text(HTMLParser):
    """HTML -> chữ thuần: bỏ script/style/noscript, giữ xuống dòng ở khối."""

    _SKIP = frozenset({"script", "style", "noscript", "svg", "template"})
    _BLOCK = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "pre", "section"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        self.parts.append(data)


def html_to_text(raw: str) -> tuple[str, str]:
    parser = _Text()
    parser.feed(raw)
    text = "".join(parser.parts)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return " ".join(parser.title.split()), "\n".join(line for line in lines if line)


_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-", "\xa0": " "})


def normalize(text: str) -> str:
    """So trích dẫn: bỏ khác biệt khoảng trắng, hoa/thường, kiểu dấu nháy và gạch."""
    return " ".join(html.unescape(text).translate(_QUOTES).lower().split())


class ResearchTools:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        resolve: Resolver = system_resolver,
        *,
        github_api: str = GITHUB_API,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=20, headers={"User-Agent": "aico-research/0.1"}
        )
        self._resolve = resolve
        self._github_api = github_api.rstrip("/")

    async def _get(self, url: str) -> tuple[httpx.Response, bytes, str]:
        """GET tự theo chuyển hướng, kiểm từng URL; đọc tối đa MAX_BYTES; tổng thời gian có trần
        (timeout của httpx tính theo từng thao tác, máy chủ nhỏ giọt có thể giữ rất lâu)."""
        try:
            async with asyncio.timeout(TOTAL_SECONDS):
                return await self._get_pinned(url)
        except TimeoutError:
            raise ResearchError(f"đọc trang quá {TOTAL_SECONDS} giây: {url}") from None

    async def _get_pinned(self, url: str) -> tuple[httpx.Response, bytes, str]:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            ip = await check_url(current, self._resolve)
            parts = urlsplit(current)
            host = parts.hostname or ""
            headers = {"Host": parts.netloc.rpartition("@")[2]}
            extensions = {"sni_hostname": host} if parts.scheme == "https" else {}
            async with self._client.stream(
                "GET",
                pinned_url(current, ip),
                headers=headers,
                extensions=extensions,
                follow_redirects=False,
            ) as resp:
                if resp.is_redirect and resp.headers.get("location"):
                    current = urljoin(current, resp.headers["location"])
                    continue
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        break
                return resp, bytes(body[:MAX_BYTES]), current
        raise ResearchError(f"chuyển hướng quá {MAX_REDIRECTS} lần: {url}")

    async def fetch_url(self, url: str, max_chars: int = DEFAULT_CHARS) -> dict[str, Any]:
        page = await self._page(url)
        text = page.pop("full_text")
        limit = max(1, min(int(max_chars), MAX_CHARS))
        return {**page, "text": text[:limit], "truncated": len(text) > limit}

    async def _page(self, url: str) -> dict[str, Any]:
        """Trang đã đọc với toàn văn (tới MAX_BYTES) trong `full_text`."""
        resp, body, final = await self._get(url)
        if resp.status_code >= 400:
            raise ResearchError(f"trang trả HTTP {resp.status_code}: {final}")
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype and not (ctype.startswith("text/") or ctype in ("application/json",)):
            raise ResearchError(f"không đọc loại nội dung {ctype} (chỉ HTML/văn bản/JSON)")
        raw = body.decode(resp.encoding or "utf-8", errors="replace")
        title, text = (
            html_to_text(raw)
            if "html" in ctype or "<html" in raw[:500].lower()
            else (
                "",
                raw,
            )
        )
        return {
            "url": url,
            "final_url": final,
            "status": resp.status_code,
            "content_type": ctype,
            "title": title,
            "full_text": text,
            "sha256": hashlib.sha256(body).hexdigest(),
            "retrieved": _now(),
        }

    async def verify_quote(self, url: str, quote: str) -> dict[str, Any]:
        """Trích dẫn có nguyên văn trên trang không (bỏ qua khoảng trắng, hoa/thường, kiểu nháy)."""
        if len(quote.split()) < 3:
            raise ResearchError("trích dẫn quá ngắn để kiểm (cần từ 3 từ)")
        page = await self._page(url)  # toàn văn: trích dẫn ở cuối changelog dài vẫn tìm được
        hay, needle = normalize(page["full_text"]), normalize(quote)
        at = hay.find(needle)
        context = hay[max(0, at - 120) : at + len(needle) + 120] if at >= 0 else ""
        return {
            "url": url,
            "final_url": page["final_url"],
            "found": at >= 0,
            "context": context,
            "sha256": page["sha256"],
            "retrieved": page["retrieved"],
        }

    async def _github(self, path: str, params: dict[str, Any]) -> Any:
        resp = await self._client.get(
            f"{self._github_api}{path}",
            params=params,
            headers={"Accept": "application/vnd.github+json"},
            follow_redirects=True,  # repo đổi tên/chuyển chủ trả 301; host GitHub cố định
        )
        if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
            raise ResearchError("GitHub hết lượt gọi không token trong giờ này, thử lại sau")
        if resp.status_code >= 400:
            raise ResearchError(f"GitHub trả HTTP {resp.status_code} cho {path}")
        return resp.json()

    async def github_releases(self, repo: str, limit: int = 5) -> dict[str, Any]:
        """Bản phát hành mới nhất của repo công khai (owner/name): tag, ngày, nội dung rút gọn."""
        if not _REPO.fullmatch(repo):
            raise ResearchError(f"repo phải dạng owner/name: {repo}")
        items = await self._github(f"/repos/{repo}/releases", {"per_page": max(1, min(limit, 20))})
        if not isinstance(items, list):
            raise ResearchError(f"GitHub trả dữ liệu lạ cho releases của {repo}")
        return {
            "repo": repo,
            "releases": [
                {
                    "tag": r.get("tag_name"),
                    "name": r.get("name"),
                    "published_at": r.get("published_at"),
                    "prerelease": r.get("prerelease"),
                    "url": r.get("html_url"),
                    "body": (r.get("body") or "")[:4000],
                }
                for r in items
                if isinstance(r, dict)
            ],
            "retrieved": _now(),
        }

    async def github_search_repos(
        self, query: str, created_after: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        """Repo công khai theo từ khoá, nhiều sao trước; created_after (YYYY-MM-DD) để xem repo mới
        nổi (xu hướng). Ghi giấy phép để biết có được tham khảo code không."""
        q = " ".join(query.split())
        if not q:
            raise ResearchError("thiếu từ khoá tìm repo")
        if created_after:
            try:
                datetime.fromisoformat(created_after)
            except ValueError as exc:
                raise ResearchError("created_after phải dạng YYYY-MM-DD") from exc
            q += f" created:>{created_after}"
        data = await self._github(
            "/search/repositories",
            {"q": q, "sort": "stars", "order": "desc", "per_page": max(1, min(limit, 30))},
        )
        return {
            "query": q,
            "repos": [
                {
                    "full_name": r.get("full_name"),
                    "description": r.get("description"),
                    "stars": r.get("stargazers_count"),
                    "created_at": r.get("created_at"),
                    "pushed_at": r.get("pushed_at"),
                    "license": (r.get("license") or {}).get("spdx_id"),
                    "url": r.get("html_url"),
                }
                for r in data.get("items", [])
                if isinstance(r, dict)
            ],
            "retrieved": _now(),
        }
