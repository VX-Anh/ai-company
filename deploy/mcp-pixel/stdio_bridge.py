"""Cầu nối MCP: Streamable HTTP (cho aico gọi) -> MCP server chạy bằng stdio (Pixelbolt).

Mỗi request mở một tiến trình stdio mới: Pixelbolt lưu revision trên đĩa (--out/.revisions),
nên không cần giữ phiên. Server chỉ nằm trên mạng core, không mở port ra máy.

Biến môi trường:
- BRIDGE_COMMAND: lệnh chạy MCP server stdio,
  ví dụ "node /opt/pixelbolt/bin/pixelbolt.js mcp --out /data/output"
- BRIDGE_NAME (mặc định "pixel"), BRIDGE_HOST (0.0.0.0), BRIDGE_PORT (8102)
"""

from __future__ import annotations

import os
import shlex
from typing import Any

import mcp_types as types
import uvicorn
from mcp import Client, StdioServerParameters
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server


def build_app(params: StdioServerParameters, name: str) -> Any:
    async def on_list_tools(
        ctx: ServerRequestContext[Any, Any], p: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        async with Client(params) as client:
            return await client.list_tools()

    async def on_call_tool(
        ctx: ServerRequestContext[Any, Any], p: types.CallToolRequestParams
    ) -> types.CallToolResult:
        async with Client(params) as client:
            return await client.call_tool(p.name, dict(p.arguments or {}))

    server: Server[Any] = Server(name, on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    return server.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")


def main() -> None:
    command = shlex.split(os.environ["BRIDGE_COMMAND"])
    params = StdioServerParameters(command=command[0], args=command[1:])
    app = build_app(params, os.environ.get("BRIDGE_NAME", "pixel"))
    uvicorn.run(
        app,
        host=os.environ.get("BRIDGE_HOST", "0.0.0.0"),
        port=int(os.environ.get("BRIDGE_PORT", "8102")),
    )


if __name__ == "__main__":
    main()
