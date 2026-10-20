# Image worker: CLI chính thức của các subscription, chạy headless theo lệnh của aico.
# Mỗi container chỉ đăng nhập một CLI; credential nằm trong volume /home/worker.
FROM node:22-bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl git ripgrep bash bubblewrap python3 tzdata \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code @openai/codex && npm cache clean --force
RUN useradd --uid 10003 --create-home --shell /bin/bash worker
USER worker
WORKDIR /home/worker
# Cursor CLI chỉ có bộ cài script, cài vào ~/.local; chép ra /opt để volume home không che mất.
RUN curl -fsSL https://cursor.com/install | bash
USER root
RUN cp -a /home/worker/.local/share/cursor-agent /opt/cursor-agent \
    && ln -s "$(readlink -f /home/worker/.local/bin/cursor-agent | sed 's#/home/worker/.local/share/cursor-agent#/opt/cursor-agent#')" /usr/local/bin/cursor-agent \
    && rm -rf /home/worker/.local
COPY deploy/worker/worker_server.py /opt/worker_server.py
USER worker
ENV PYTHONUNBUFFERED=1
CMD ["python3", "/opt/worker_server.py"]
