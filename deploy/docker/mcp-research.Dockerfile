# Image cho MCP server nghiên cứu. Không giữ secret; mạng riêng ra Internet, không thấy mạng core.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
COPY mcp_servers mcp_servers
COPY src src
RUN uv sync --frozen --no-dev --package mcp-research

FROM python:3.12-slim-bookworm
RUN useradd --uid 10004 --create-home mcp
WORKDIR /app
COPY --from=build /app /app
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 MCP_HOST=0.0.0.0 MCP_PORT=8104
USER mcp
CMD ["mcp-research"]
