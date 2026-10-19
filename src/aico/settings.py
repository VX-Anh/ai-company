"""Cấu hình chạy từ biến môi trường. Secret đọc từ file (Docker secrets), không để trong env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _secret(name: str) -> str | None:
    path = os.environ.get(name)
    if not path:
        return None
    return Path(path).read_text(encoding="utf-8").strip() or None


def _list(name: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in os.environ.get(name, "").split(",") if p.strip())


@dataclass(frozen=True)
class Settings:
    database_url: str
    company_dir: Path = Path("company")
    mcp_gitlab_url: str = "http://127.0.0.1:8101/mcp"
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
    planner: str = "static"  # static | llm

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        return cls(
            database_url=env["AICO_DATABASE_URL"],
            company_dir=Path(env.get("AICO_COMPANY_DIR", "company")),
            mcp_gitlab_url=env.get("AICO_MCP_GITLAB_URL", cls.mcp_gitlab_url),
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
        )
