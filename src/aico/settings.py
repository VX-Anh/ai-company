"""Cấu hình chạy từ biến môi trường. Secret đọc từ file (Docker secrets), không để trong env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


def _secret(name: str) -> str | None:
    path = os.environ.get(name)
    if not path:
        return None
    return Path(path).read_text(encoding="utf-8").strip() or None


def _list(name: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in os.environ.get(name, "").split(",") if p.strip())


def _mapping(name: str) -> dict[str, str]:
    """`a=url1,b=url2` -> {a: url1, b: url2}."""
    pairs = (p.split("=", 1) for p in _list(name) if "=" in p)
    return {k.strip(): v.strip() for k, v in pairs}


@dataclass(frozen=True)
class Settings:
    database_url: str
    company_dir: Path = Path("company")
    mcp_gitlab_url: str = "http://127.0.0.1:8101/mcp"
    mcp_pixel_url: str | None = None  # Pixelbolt (pixel art); không đặt thì không có tool pixel
    mcp_rea_url: str | None = None  # REA (phân tích ngược); không đặt thì không có tool rea
    # Nghiên cứu (fetch_url, verify_quote, GitHub): không đặt thì không có tool research
    mcp_research_url: str | None = None
    upstream_llm_url: str = "http://127.0.0.1:20128/v1"
    upstream_llm_key: str | None = None
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    relay_host: str = "127.0.0.1"
    relay_port: int = 8200
    api_token: str | None = None
    internal_token: str | None = None
    intake_projects: tuple[str, ...] = ()
    intake_label: str = "ai-company"
    poll_seconds: float = 60
    lease_seconds: float = 60
    default_project: str | None = None
    planner: str = "static"  # static | llm | cli
    coder: str = "static"  # static (ScriptedCoder) | cli
    reviewer: str = "static"  # static (ScriptedReviewer) | cli
    runner_concurrency: int = 3  # số ticket chạy song song (mỗi vòng runner một ticket)
    # Worker CLI subscription: tên -> URL worker server; token chung giữa aico và worker.
    workers: dict[str, str] = field(default_factory=dict)
    worker_token: str | None = None
    bridge_host: str = "127.0.0.1"
    bridge_port: int = 8300
    bridge_url: str = "http://127.0.0.1:8300/mcp"  # địa chỉ worker dùng để gọi tool bridge
    # Phòng có cửa sổ hạn mức đã dùng từ mức này (%) được giao việc sau phòng còn thoải mái.
    usage_tight_percent: float = 80
    # Phòng chạy chậm: đầu việc găng (đầu việc khác đang chờ) giao sau các phòng còn lại.
    slow_workers: tuple[str, ...] = ("cursor",)
    # Máy build từ xa (deploy/remote): agent chạy build/test qua aico-remote, MR của các repo
    # trong preview_projects được dựng bản xem trước. Không đặt thì giữ cách cũ.
    remote_url: str | None = None
    preview_projects: tuple[str, ...] = ()
    # Chu kỳ (giây) kiểm tra branch đích của preview_projects để dựng lại bản chính; 0 là tắt.
    site_watch_seconds: float = 60
    # Dò công nghệ tự động (Phòng R&D): mỗi N ngày, khi hệ rảnh và hạn mức còn thấp; 0 là tắt.
    rnd_scan_interval_days: int = 0
    # Instance chạy lại của Phòng R&D (service aico-eval): runner chỉ nhận ticket nhãn eval và
    # không chạy intake, Jira, site watcher, dò công nghệ (aico chính đã làm).
    eval_instance: bool = False
    # Biến thể mà instance eval đang mount (AICO_COMPANY_DIR): chỉ nhận ticket của biến thể đó.
    eval_variant: str | None = None
    # Cổng hạn mức của instance eval: trần số ticket chạy lại bắt đầu mỗi 7 ngày (0: không chạy
    # ticket mới) và ngưỡng % hạn mức các phòng (cửa sổ 5 giờ / tuần); vượt thì chờ.
    eval_weekly_cap: int = 40
    eval_max_session_percent: float = 70
    eval_max_week_percent: float = 60
    # Kho tri thức Phòng R&D: librarian dựng chỉ mục từ main mỗi knowledge_watch_seconds; agent
    # có tool knowledge.search/get và khối <knowledge> trong prompt. Rỗng là tắt.
    knowledge_project: str | None = "VX-Anh/aico-knowledge"
    knowledge_watch_seconds: float = 60
    # Đồng bộ ticket sang Jira (một chiều). Bật khi đủ URL, email, token và ánh xạ
    # repo -> project Jira (`VX-Anh/linh-hoa-vien=LHV`). Ticket cập nhật trước jira_since bỏ qua.
    jira_url: str | None = None
    jira_email: str | None = None
    jira_token: str | None = None
    jira_projects: dict[str, str] = field(default_factory=dict)
    jira_since: datetime | None = None
    cors_origins: tuple[str, ...] = ()  # GUI web, ví dụ http://localhost:5173
    # Mặc định cho GUI chạy ở mọi cổng trên máy này (dev server tự đổi cổng khi bị chiếm).
    cors_origin_regex: str | None = r"^http://(localhost|127\.0\.0\.1)(:\d+)?$"

    @property
    def jira_enabled(self) -> bool:
        return bool(self.jira_url and self.jira_email and self.jira_token and self.jira_projects)

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        return cls(
            database_url=env["AICO_DATABASE_URL"],
            company_dir=Path(env.get("AICO_COMPANY_DIR", "company")),
            mcp_gitlab_url=env.get("AICO_MCP_GITLAB_URL", cls.mcp_gitlab_url),
            mcp_pixel_url=env.get("AICO_MCP_PIXEL_URL") or None,
            mcp_rea_url=env.get("AICO_MCP_REA_URL") or None,
            mcp_research_url=env.get("AICO_MCP_RESEARCH_URL") or None,
            upstream_llm_url=env.get("AICO_UPSTREAM_LLM_URL", cls.upstream_llm_url),
            upstream_llm_key=_secret("AICO_UPSTREAM_LLM_KEY_FILE"),
            api_host=env.get("AICO_API_HOST", cls.api_host),
            api_port=int(env.get("AICO_API_PORT", cls.api_port)),
            relay_host=env.get("AICO_RELAY_HOST", cls.relay_host),
            relay_port=int(env.get("AICO_RELAY_PORT", cls.relay_port)),
            api_token=_secret("AICO_API_TOKEN_FILE"),
            internal_token=_secret("AICO_INTERNAL_TOKEN_FILE"),
            intake_projects=_list("AICO_INTAKE_PROJECTS"),
            intake_label=env.get("AICO_INTAKE_LABEL", cls.intake_label),
            poll_seconds=float(env.get("AICO_POLL_SECONDS", cls.poll_seconds)),
            lease_seconds=float(env.get("AICO_LEASE_SECONDS", cls.lease_seconds)),
            default_project=env.get("AICO_DEFAULT_PROJECT"),
            planner=env.get("AICO_PLANNER", cls.planner),
            coder=env.get("AICO_CODER", cls.coder),
            reviewer=env.get("AICO_REVIEWER", cls.reviewer),
            runner_concurrency=max(
                1, int(env.get("AICO_RUNNER_CONCURRENCY", cls.runner_concurrency))
            ),
            workers=_mapping("AICO_WORKERS"),
            worker_token=_secret("AICO_WORKER_TOKEN_FILE"),
            bridge_host=env.get("AICO_BRIDGE_HOST", cls.bridge_host),
            bridge_port=int(env.get("AICO_BRIDGE_PORT", cls.bridge_port)),
            bridge_url=env.get("AICO_BRIDGE_URL", cls.bridge_url),
            usage_tight_percent=float(env.get("AICO_USAGE_TIGHT_PERCENT", cls.usage_tight_percent)),
            slow_workers=(
                _list("AICO_SLOW_WORKERS") if "AICO_SLOW_WORKERS" in env else cls.slow_workers
            ),
            remote_url=env.get("AICO_REMOTE_URL") or None,
            preview_projects=_list("AICO_PREVIEW_PROJECTS"),
            site_watch_seconds=float(env.get("AICO_SITE_WATCH_SECONDS", cls.site_watch_seconds)),
            rnd_scan_interval_days=int(env.get("AICO_RND_SCAN_INTERVAL_DAYS", "0") or 0),
            eval_instance=env.get("AICO_EVAL_INSTANCE", "") == "1",
            eval_variant=env.get("AICO_EVAL_VARIANT") or None,
            eval_weekly_cap=int(env.get("AICO_EVAL_WEEKLY_CAP") or cls.eval_weekly_cap),
            eval_max_session_percent=float(
                env.get("AICO_EVAL_MAX_SESSION_PERCENT") or cls.eval_max_session_percent
            ),
            eval_max_week_percent=float(
                env.get("AICO_EVAL_MAX_WEEK_PERCENT") or cls.eval_max_week_percent
            ),
            knowledge_project=env.get("AICO_KNOWLEDGE_PROJECT", cls.knowledge_project or "")
            or None,
            knowledge_watch_seconds=float(
                env.get("AICO_KNOWLEDGE_WATCH_SECONDS", cls.knowledge_watch_seconds)
            ),
            jira_url=env.get("AICO_JIRA_URL") or None,
            jira_email=env.get("AICO_JIRA_EMAIL") or None,
            jira_token=_secret("AICO_JIRA_TOKEN_FILE"),
            jira_projects=_mapping("AICO_JIRA_PROJECTS"),
            jira_since=(
                datetime.fromisoformat(env["AICO_JIRA_SINCE"])
                if env.get("AICO_JIRA_SINCE")
                else None
            ),
            cors_origins=_list("AICO_CORS_ORIGINS"),
            cors_origin_regex=env.get("AICO_CORS_ORIGIN_REGEX", cls.cors_origin_regex) or None,
        )
