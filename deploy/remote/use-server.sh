#!/usr/bin/env bash
# Chuyển build/test của agent giữa server 14 (qua VPN) và Docker trên PC.
#   bash use-server.sh ssh [user@host]   # mặc định aico@192.168.150.47 (IP LAN, vào qua OpenVPN)
#   bash use-server.sh local             # quay về build-docker trên PC (khi VPN/server không vào được)
# Chỉ sửa các dòng REMOTE_BACKEND, REMOTE_SSH_TARGET, REMOTE_MEMORY, REMOTE_CONCURRENCY,
# COMPOSE_PROFILES trong .env (có bản sao .env.bak-*), không in nội dung .env.
set -euo pipefail
STACK=${STACK:-$HOME/ai-company/deploy}
MODE=${1:?ssh|local}
TARGET=${2:-aico@192.168.150.47}
# Dấu vân tay khoá máy server 14 (ECDSA), đối chiếu với known_hosts Windows ngày 17/11/2026.
FP=SHA256:k4SrQ+MAnsjVRl9e/rDWSB4jJPMYB1gPTSzLIbf1/BU
cd "$STACK"

set_kv() {  # set_kv KEY VALUE; VALUE rỗng = xoá dòng
    if [ -z "$2" ]; then sed -i "/^$1=/d" .env
    elif grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env
    else printf '%s=%s\n' "$1" "$2" >> .env; fi
}
profiles() {  # profiles add|del localbuild
    local cur new
    cur=$(sed -n 's/^COMPOSE_PROFILES=//p' .env | tr -d '"')
    new=$(printf '%s' "$cur" | tr ',' '\n' | grep -vx "localbuild" | grep -v '^$' || true)
    [ "$1" = add ] && new=$(printf '%s\nlocalbuild' "$new")
    set_kv COMPOSE_PROFILES "$(printf '%s' "$new" | grep -v '^$' | paste -sd, -)"
}

cp .env ".env.bak-$(date +%Y%m%d-%H%M%S)"
if [ "$MODE" = ssh ]; then
    host=${TARGET#*@}
    timeout 8 bash -c "</dev/tcp/$host/22" 2>/dev/null || { echo "Không vào được $host:22 (VPN đã bật chưa?)"; exit 1; }
    if ! ssh-keygen -F "$host" -f secrets/remote_known_hosts >/dev/null; then
        line=$(ssh-keyscan -T 8 -t ecdsa "$host" 2>/dev/null)
        got=$(printf '%s\n' "$line" | ssh-keygen -lf - | awk '{print $2}')
        [ "$got" = "$FP" ] || { echo "Khoá máy $host ($got) KHÁC server 14, dừng."; exit 1; }
        printf '%s\n' "$line" >> secrets/remote_known_hosts
        echo "Đã thêm khoá máy $host vào known_hosts."
    fi
    set_kv REMOTE_BACKEND ssh
    set_kv REMOTE_SSH_TARGET "$TARGET"
    set_kv REMOTE_MEMORY ""        # về mặc định compose (6g, 4 job): server 124 GB RAM
    set_kv REMOTE_CONCURRENCY ""
    profiles del
    docker compose up -d --no-deps --force-recreate remote-runner
    docker compose stop build-docker 2>/dev/null || true   # giữ volume cache để quay về local nhanh
elif [ "$MODE" = local ]; then
    set_kv REMOTE_BACKEND local
    set_kv REMOTE_MEMORY 3g
    set_kv REMOTE_CONCURRENCY 2
    profiles add
    docker compose up -d build-docker
    docker compose up -d --no-deps --force-recreate remote-runner
else
    echo "MODE phải là ssh hoặc local"; exit 2
fi
sleep 3
docker compose exec -T remote-runner python3 -c \
  'import urllib.request;print(urllib.request.urlopen("http://127.0.0.1:8500/health").read().decode())'
if [ "$MODE" = ssh ]; then
    docker compose exec -T remote-runner sh -c \
      'ssh -i "$REMOTE_SSH_KEY_FILE" -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
         -o UserKnownHostsFile="$REMOTE_KNOWN_HOSTS_FILE" -o ConnectTimeout=10 "$REMOTE_SSH_TARGET" \
         "docker ps --format {{.Names}} | head -5; docker images --format {{.Repository}}:{{.Tag}} | grep -E \"node-playwright|uv\" | head -3; nproc; free -g | sed -n 2p"'
fi
