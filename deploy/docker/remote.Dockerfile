# Máy build từ xa: điều khiển Docker trên server qua SSH. Chỉ giữ khoá SSH tới server
# (user không có quyền gì ngoài Docker rootless của nó), không giữ secret nào khác của aico.
FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --uid 10004 --create-home --shell /usr/sbin/nologin runner \
    && mkdir -p /data/previews && chown runner /data /data/previews
COPY deploy/remote/remote_runner.py /opt/remote_runner.py
USER runner
ENV PYTHONUNBUFFERED=1
CMD ["python3", "/opt/remote_runner.py"]
