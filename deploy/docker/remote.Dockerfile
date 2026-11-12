# Máy build: điều khiển Docker trên server qua SSH, hoặc Docker build riêng trên PC
# (REMOTE_BACKEND=local, service build-docker). Chỉ giữ khoá SSH tới server, không giữ secret
# nào khác của aico.
FROM python:3.12-slim
COPY --from=docker:29-cli /usr/local/bin/docker /usr/local/bin/docker
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --uid 10004 --create-home --shell /usr/sbin/nologin runner \
    && mkdir -p /data/previews && chown runner /data /data/previews
COPY deploy/remote/remote_runner.py /opt/remote_runner.py
USER runner
ENV PYTHONUNBUFFERED=1
CMD ["python3", "/opt/remote_runner.py"]
