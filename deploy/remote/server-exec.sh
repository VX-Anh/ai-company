#!/usr/bin/env bash
# Chạy một lệnh trên server build qua khoá của remote-runner (cùng cấu hình SSH như runner).
#   bash server-exec.sh 'docker ps -a; uptime'
set -euo pipefail
cd "${STACK:-$HOME/ai-company/deploy}"
docker compose exec -T -e CMD="$1" remote-runner sh -c 'exec ssh -i "$REMOTE_SSH_KEY_FILE" -o BatchMode=yes \
  -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$REMOTE_KNOWN_HOSTS_FILE" \
  -o ConnectTimeout=10 "$REMOTE_SSH_TARGET" "$CMD"'
