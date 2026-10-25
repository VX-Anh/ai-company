# MCP server REA (Reverse Engineer Anything, MIT): phân tích tĩnh binary, gói ứng dụng, JS/Electron,
# firmware. rea-agents 6.7.0 chỉ có transport stdio (`rea --mcp`) nên chạy sau cầu nối stdio -> HTTP
# dùng chung với Pixelbolt; mỗi request một tiến trình REA, tool nào cũng nêu target trong tham số.
# Chỉ nằm trên mạng core: aico gọi qua gateway + guard + audit. Không có secret nào của aico.
# File cần phân tích: thư mục targets gắn chỉ đọc vào /targets.
FROM node:22-bookworm-slim
ARG REA_VERSION=6.7.0
# procps: REA trên Linux cần `ps` để kiểm tra tiến trình con trước khi chạy engine.
# binutils: objdump cho inspect_with_objdump (header, symbol, disassemble ELF/PE).
# make/g++: phòng khi node-pty không có bản dựng sẵn cho máy.
RUN apt-get update && apt-get install -y --no-install-recommends \
        procps binutils python3 python3-venv make g++ ca-certificates \
    && npm install -g "rea-agents@${REA_VERSION}" \
    && npm cache clean --force \
    && python3 -m venv /opt/bridge \
    && /opt/bridge/bin/pip install --no-cache-dir "mcp==2.3.0" "uvicorn>=0.30" \
    && apt-get purge -y make g++ && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
COPY deploy/mcp-pixel/stdio_bridge.py /opt/stdio_bridge.py
RUN useradd --uid 10005 --create-home rea && mkdir -p /targets /data && chown rea /data
USER rea
WORKDIR /data
ENV PYTHONUNBUFFERED=1 \
    XDG_CACHE_HOME=/data/cache \
    BRIDGE_NAME=rea \
    BRIDGE_PORT=8103 \
    BRIDGE_COMMAND="rea --mcp"
CMD ["/opt/bridge/bin/python", "/opt/stdio_bridge.py"]
