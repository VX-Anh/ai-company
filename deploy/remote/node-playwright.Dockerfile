# Image mặc định của máy build từ xa cho repo Node: node:22 kèm thư viện hệ thống Chromium cần,
# để e2e Playwright chạy được ở pha run (không mạng). Bản trình duyệt khớp phiên bản Playwright
# của từng repo được pha setup tải vào cache dùng chung (/cache/ms-playwright), không nằm ở đây.
# Dựng trên server: aico-ops.sh remote-image (gửi file này qua ssh, docker build trên server).
FROM node:22-bookworm-slim
RUN npx -y playwright@1.64.1 install-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.npm
