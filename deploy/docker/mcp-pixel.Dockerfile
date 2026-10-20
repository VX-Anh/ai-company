# MCP server pixel art: Pixelbolt (trước là PixelForge, MIT, chạy offline, không cần API key)
# sau cầu nối stdio -> Streamable HTTP. Container riêng, không có secret nào của aico.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
ARG PIXELBOLT_REF=v0.9.0
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && git clone --depth 1 --branch "${PIXELBOLT_REF}" https://github.com/skulitom/Pixelbolt.git /opt/pixelbolt \
    && rm -rf /opt/pixelbolt/.git \
    && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
RUN uv pip install --system --no-cache "mcp==2.3.0" "uvicorn>=0.30"
COPY deploy/mcp-pixel/stdio_bridge.py /opt/stdio_bridge.py
RUN useradd --uid 10004 --create-home pixel && mkdir -p /data && chown pixel /data
USER pixel
WORKDIR /data
ENV PYTHONUNBUFFERED=1 \
    BRIDGE_NAME=pixel \
    BRIDGE_COMMAND="node /opt/pixelbolt/bin/pixelbolt.js mcp --out /data/output"
CMD ["python", "/opt/stdio_bridge.py"]
