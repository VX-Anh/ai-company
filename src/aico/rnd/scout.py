"""Dò công nghệ định kỳ (Phòng R&D): dựng ticket `research` từ `company/rnd/watchlist.yaml`.

Bản tay (R0b): owner chạy `aico-ops scan [ngày]`. Bản tự động (R2) sẽ do bộ lập lịch gọi cùng hàm
này khi hệ rảnh. Kết quả là thẻ tri thức cho mục đáng thử, mỗi thẻ mang một giả thuyết đo được,
và một "Báo cáo xu hướng" trong kế hoạch của Research Lead (lưu ở docs/plans/ của kho tri thức).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

RESEARCH_LABEL = "research"


class WatchlistError(ValueError):
    pass


@dataclass(frozen=True)
class Watchlist:
    goals: dict[str, str]
    release_repos: tuple[tuple[str, str], ...]  # (owner/name, goal)
    changelog_urls: tuple[tuple[str, str], ...]  # (url, goal)
    search_queries: tuple[tuple[str, str], ...]  # (từ khoá, goal)


def _pairs(data: dict[str, Any], key: str, field: str, goals: dict[str, str]) -> tuple:
    out: list[tuple[str, str]] = []
    for i, item in enumerate(data.get(key) or [], 1):
        if not isinstance(item, dict) or not str(item.get(field) or "").strip():
            raise WatchlistError(f"{key}[{i}]: thiếu '{field}'")
        goal = str(item.get("goal") or "")
        if goal not in goals:
            raise WatchlistError(f"{key}[{i}]: goal '{goal}' không có trong goals")
        out.append((str(item[field]).strip(), goal))
    return tuple(out)


def load_watchlist(path: Path) -> Watchlist:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise WatchlistError(f"không đọc được {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise WatchlistError(f"{path}: phải là mapping YAML")
    goals = {
        str(g["id"]): " ".join(str(g.get("text") or "").split())
        for g in data.get("goals") or []
        if isinstance(g, dict) and g.get("id")
    }
    if not goals:
        raise WatchlistError(f"{path}: cần ít nhất một goal")
    return Watchlist(
        goals=goals,
        release_repos=_pairs(data, "release_repos", "repo", goals),
        changelog_urls=_pairs(data, "changelog_urls", "url", goals),
        search_queries=_pairs(data, "search_queries", "query", goals),
    )


def scan_ticket(wl: Watchlist, *, today: date, days: int, project: str) -> dict[str, Any]:
    """Ticket research dò các nguồn trong `days` ngày gần nhất (dữ liệu cho POST /tickets)."""
    since = (today - timedelta(days=days)).isoformat()
    lines = [
        f"Dò công nghệ định kỳ của Phòng R&D, kỳ {since} → {today.isoformat()}.",
        "",
        "Mục tiêu (mỗi phát hiện phải gắn với một mục tiêu):",
        *(f"- {gid}: {text}" for gid, text in wl.goals.items()),
        "",
        "Nguồn phải dò (chỉ những gì phát hành/tạo từ ngày " + since + "):",
        "- Bản phát hành (tool github_releases):",
        *(f"  - {repo} [{goal}]" for repo, goal in wl.release_repos),
        "- Changelog (tool fetch_url):",
        *(f"  - {url} [{goal}]" for url, goal in wl.changelog_urls),
        f"- Repo mới nổi (tool github_search_repos, created_after={since}):",
        *(f'  - "{q}" [{goal}]' for q, goal in wl.search_queries),
        "",
        "Yêu cầu:",
        "- Research Lead: trong kế hoạch viết mục **Báo cáo xu hướng** gồm ba phần: Mới (tóm "
        "tắt mỗi nguồn có gì mới, kèm ngày), Đáng thử (xếp theo: liên quan tới mục tiêu, có số "
        "đo hay bằng chứng, chi phí thử), Bỏ qua (kèm lý do). Nguồn không có gì mới thì ghi "
        "'không có gì mới'.",
        "- Chỉ mục **Đáng thử** mới thành thẻ (tối đa 5). Mỗi thẻ có ở mục 'Áp dụng cho aico / dự "
        "án' một giả thuyết kiểm chứng được: nếu áp X vào vai trò/bước Y thì chỉ số Z đổi theo "
        "hướng ..., đo bằng ... (event job, /rnd/metrics, bộ ticket chuẩn).",
        "- Repo tham khảo phải ghi giấy phép. Không bịa phiên bản hay tính năng; không chắc "
        "thì bỏ.",
        "- Đã có thẻ cùng chủ đề trong INDEX.md thì cập nhật thẻ đó (tăng version).",
    ]
    return {
        "title": f"Dò công nghệ {today.isoformat()} ({days} ngày)",
        "project": project,
        "labels": [RESEARCH_LABEL, "scan"],
        "body": "\n".join(lines),
    }
