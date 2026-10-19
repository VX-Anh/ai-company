# Image cho aico (API, model relay, runner, intake). Build từ thư mục gốc repo.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
COPY mcp_servers/gitlab/pyproject.toml mcp_servers/gitlab/pyproject.toml
RUN uv sync --frozen --no-dev --package aico --no-install-workspace
COPY src src
COPY mcp_servers mcp_servers
RUN uv sync --frozen --no-dev --package aico

FROM python:3.12-slim-bookworm
RUN useradd --uid 10001 --create-home aico
WORKDIR /app
COPY --from=build /app /app
COPY company /app/company
ENV PATH=/app/.venv/bin:$PATH AICO_COMPANY_DIR=/app/company PYTHONUNBUFFERED=1
USER aico
CMD ["aico", "serve"]
