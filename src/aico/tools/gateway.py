"""Cửa duy nhất để agent và aico gọi tool: guard trước, audit sau, MCP ở giữa."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from mcp.client.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent

from aico.core import ActionLevel
from aico.governance import AuditEntry, AuditLog, mask_secrets
from aico.registry import Company
from aico.tools.guard import Guard, Verdict


class ToolError(RuntimeError):
    """Tool chạy nhưng trả lỗi (ví dụ GitLab từ chối)."""


class ToolDenied(PermissionError):
    pass


class ApprovalRequired(PermissionError):
    pass


@dataclass(frozen=True)
class ToolSpec:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]


class ToolClient(Protocol):
    async def call(self, server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]: ...

    async def list_tools(self, server: str) -> list[ToolSpec]: ...


def _result_to_dict(result: CallToolResult) -> dict[str, Any]:
    texts = [c.text for c in result.content if isinstance(c, TextContent)]
    if result.is_error:
        raise ToolError("; ".join(texts) or "tool trả lỗi")
    if isinstance(result.structured_content, dict):
        return result.structured_content
    if texts:
        try:
            parsed = json.loads(texts[0])
        except json.JSONDecodeError:
            return {"text": "\n".join(texts)}
        return parsed if isinstance(parsed, dict) else {"result": parsed}
    return {}


class McpToolClient:
    """Gọi MCP server qua Streamable HTTP (URL) hoặc trong cùng tiến trình (MCPServer, khi test)."""

    def __init__(self, servers: Mapping[str, str | MCPServer]) -> None:
        self._servers = dict(servers)

    async def call(self, server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        target = self._servers.get(server)
        if target is None:
            raise ToolError(f"không có MCP server '{server}'")
        async with Client(target) as client:
            result = await client.call_tool(tool, args)
        return _result_to_dict(result)

    async def list_tools(self, server: str) -> list[ToolSpec]:
        target = self._servers.get(server)
        if target is None:
            raise ToolError(f"không có MCP server '{server}'")
        async with Client(target) as client:
            listed = await client.list_tools()
        return [
            ToolSpec(server, t.name, t.description or "", dict(t.input_schema))
            for t in listed.tools
        ]


def _summary(result: dict[str, Any]) -> str:
    text = json.dumps(mask_secrets(result), ensure_ascii=False, default=str)
    return text if len(text) <= 300 else text[:300] + "…"


class ToolGateway:
    def __init__(self, company: Company, guard: Guard, client: ToolClient, audit: AuditLog) -> None:
        self._company = company
        self._guard = guard
        self._client = client
        self._audit = audit

    async def tools_for(self, role_id: str) -> list[ToolSpec]:
        """Tool agent của vai trò được thấy: đã cấp trong YAML và dưới mức L3.

        Hành động L3 (merge) không bao giờ giao cho agent; aico tự gọi sau khi người duyệt.
        """
        role = self._company.role(role_id)
        specs: list[ToolSpec] = []
        for grant in role.tools:
            allowed = set(grant.allow)
            for spec in await self._client.list_tools(grant.server):
                level = self._company.policy.level_of(grant.server, spec.name)
                if spec.name in allowed and level is not None and level.rank < ActionLevel.L3.rank:
                    specs.append(spec)
        return specs

    async def call(
        self,
        role_id: str,
        server: str,
        tool: str,
        args: dict[str, Any],
        *,
        ticket_id: str | None,
    ) -> dict[str, Any]:
        role = self._company.role(role_id)
        decision = await self._guard.check(role, server, tool, args, ticket_id=ticket_id)
        entry = {
            "role": role_id,
            "server": server,
            "tool": tool,
            "args": mask_secrets(args),
            "ticket_id": ticket_id,
            "level": decision.level.value if decision.level else None,
        }
        if decision.verdict is Verdict.DENY:
            await self._audit.record(AuditEntry(decision="deny", error=decision.reason, **entry))
            raise ToolDenied(decision.reason)
        if decision.verdict is Verdict.NEEDS_APPROVAL:
            await self._audit.record(
                AuditEntry(decision="needs_approval", error=decision.reason, **entry)
            )
            raise ApprovalRequired(decision.reason)
        try:
            result = await self._client.call(server, tool, args)
        except Exception as exc:
            await self._audit.record(AuditEntry(decision="allow", error=str(exc), **entry))
            raise
        await self._audit.record(
            AuditEntry(decision="allow", result_summary=_summary(result), **entry)
        )
        return result
