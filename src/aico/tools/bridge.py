"""Tool bridge: MCP server của aico cho worker CLI (Claude Code, Codex, Cursor).

Worker không gọi thẳng mcp-gitlab. Nó gọi bridge bằng token tạm của task; bridge chỉ cho thấy
tool vai trò được cấp (dưới L3) và mọi lời gọi đi qua ToolGateway (guard + audit).
"""

from __future__ import annotations

import json
from typing import Any

import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from starlette.applications import Starlette
from starlette.types import ASGIApp, Receive, Scope, Send

from aico.llm_relay import TaskGrant, TaskTokens
from aico.tools.gateway import (
    IMAGES_KEY,
    ApprovalRequired,
    ToolDenied,
    ToolError,
    ToolGateway,
    ToolSpec,
)


def _bearer(headers: Any) -> str | None:
    value = headers.get("authorization") if headers is not None else None
    if not value or not value.lower().startswith("bearer "):
        return None
    return value[7:].strip() or None


def _text(payload: dict[str, Any], *, error: bool = False) -> types.CallToolResult:
    images = payload.get(IMAGES_KEY, [])
    data = {k: v for k, v in payload.items() if k != IMAGES_KEY}
    content: list[types.TextContent | types.ImageContent] = [
        types.TextContent(type="text", text=json.dumps(data, ensure_ascii=False, default=str))
    ]
    content += [
        types.ImageContent(type="image", data=i["data"], mime_type=i["mime_type"]) for i in images
    ]
    return types.CallToolResult(
        content=content,  # type: ignore[arg-type]
        structured_content=None if error else data,
        is_error=error,
    )


class ToolBridge:
    def __init__(self, gateway: ToolGateway, tokens: TaskTokens) -> None:
        self._gateway = gateway
        self._tokens = tokens

    def grant(self, token: str | None) -> TaskGrant | None:
        return self._tokens.resolve(token) if token else None

    async def list_tools(self, grant: TaskGrant) -> list[ToolSpec]:
        return await self._gateway.tools_for(grant.role)

    async def call_tool(
        self, grant: TaskGrant, name: str, args: dict[str, Any]
    ) -> types.CallToolResult:
        spec = next((s for s in await self.list_tools(grant) if s.name == name), None)
        if spec is None:
            return _text({"error": f"không có tool '{name}'"}, error=True)
        try:
            result = await self._gateway.call(
                grant.role, spec.server, spec.name, args, ticket_id=grant.ticket_id
            )
        except (ToolDenied, ApprovalRequired) as exc:
            return _text({"error": f"bị từ chối: {exc}"}, error=True)
        except ToolError as exc:
            return _text({"error": str(exc)}, error=True)
        return _text(result)


class _RequireToken:
    """Từ chối ngay ở tầng HTTP nếu token không hợp lệ, trước khi vào MCP."""

    def __init__(self, app: ASGIApp, bridge: ToolBridge) -> None:
        self._app = app
        self._bridge = bridge

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            if self._bridge.grant(_bearer(headers)) is None:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self._app(scope, receive, send)


def create_tool_bridge_app(bridge: ToolBridge) -> Starlette:
    def grant_of(ctx: ServerRequestContext[Any, Any]) -> TaskGrant:
        grant = bridge.grant(_bearer(getattr(ctx.request, "headers", None)))
        if grant is None:
            raise PermissionError("token task không hợp lệ hoặc đã hết hạn")
        return grant

    async def on_list_tools(
        ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        specs = await bridge.list_tools(grant_of(ctx))
        return types.ListToolsResult(
            tools=[
                types.Tool(name=s.name, description=s.description, input_schema=s.input_schema)
                for s in specs
            ]
        )

    async def on_call_tool(
        ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        return await bridge.call_tool(grant_of(ctx), params.name, dict(params.arguments or {}))

    server: Server[Any] = Server(
        "aico",
        instructions="Tool của AI Company OS. Mọi lời gọi được kiểm tra quyền và ghi audit.",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
    app = server.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")
    app.add_middleware(_RequireToken, bridge=bridge)
    return app
