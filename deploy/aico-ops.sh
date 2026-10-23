#!/bin/bash
# Lệnh vận hành stack aico trong WSL (user aico). Gọi từ Windows:
#   wsl -d Ubuntu -u aico -- bash /mnt/d/TVPL/Marshy/ai-company/deploy/aico-ops.sh <lệnh> [tham số]
# Token API đọc từ secrets/, không in ra.
set -u
SRC=/mnt/d/TVPL/Marshy/ai-company
STACK=~/ai-company/deploy
API=http://127.0.0.1:8000

api() {  # api <METHOD> <path> [json]
    local args=(-s -X "$1" -H "Authorization: Bearer $(cat "$STACK/secrets/aico_api_token")")
    [ $# -ge 3 ] && args+=(-H "Content-Type: application/json" --data-binary "$3")
    curl "${args[@]}" "$API$2"
}

# Ticket đang ở bước có agent chạy: deploy lúc này sẽ cắt ngang job.
safe() {
    local busy_tickets busy_agents
    busy_tickets=$(api GET /tickets | python3 -c '
import sys, json
print(sum(1 for t in json.load(sys.stdin)
          if t["status"] in ("new", "triaged", "planned", "in_progress", "in_review", "in_testing")))')
    busy_agents=$(api GET /workers | python3 -c '
import sys, json
print(sum(len(w.get("agents") or []) for w in json.load(sys.stdin)))')
    if [ "$busy_tickets" = 0 ] && [ "$busy_agents" = 0 ]; then
        echo yes
    else
        echo "no tickets=$busy_tickets agents=$busy_agents"
        return 1
    fi
}

deploy() {
    if [ "${1:-}" != "--force" ] && ! safe; then
        echo "Đang có job chạy; đợi xong hoặc dùng: deploy --force" >&2
        return 1
    fi
    # Không dùng git checkout/pull trong bản sao WSL: nó từng đưa file về commit cũ.
    (cd "$SRC" && tar --exclude=./.git --exclude=./.venv --exclude=./.pytest_cache \
        --exclude=./.ruff_cache --exclude=./.ssh --exclude=./.claude --exclude=./deploy/secrets \
        --exclude=./deploy/.env --exclude='__pycache__' -cf - .) | tar -xf - -C ~/ai-company
    cd "$STACK" || return 1
    docker compose up -d --build 2>&1 | grep -E 'Started|rror'
    # Cache build cũ làm ổ ảo WSL phình (từng lên 23 GB): chỉ giữ 3 ngày gần nhất.
    docker builder prune -f --filter until=72h >/dev/null
    sleep 6
    docker compose ps --format '{{.Service}} {{.Status}}'
}

tickets() {  # tickets [số lượng]
    api GET /tickets | python3 -c '
import sys, json
n = int(sys.argv[1])
for t in sorted(json.load(sys.stdin), key=lambda t: t.get("updated_at") or "")[-n:]:
    mr = [a.get("url") for a in t.get("artifacts", []) if a.get("kind") == "merge_request"]
    print(t["id"], t["status"], (t.get("updated_at") or "")[:19], "|", t["title"][:60], "|",
          mr[-1] if mr else "")' "${1:-10}"
}

ticket() {  # ticket <id>: trạng thái, artifact và các event gần nhất kèm ghi chú
    api GET "/tickets/$1" | python3 -c '
import sys, json
t = json.load(sys.stdin)
print(t["status"], "|", t["title"])
for a in t.get("artifacts", []):
    print("  artifact", a["kind"], a["ref"], a.get("url") or "")'
    api GET "/tickets/$1/events" | python3 -c '
import sys, json
for e in json.load(sys.stdin)[-12:]:
    d = e.get("data") or {}
    note = d.get("review_notes") or d.get("reason") or d.get("note") or d.get("summary") or ""
    move = e["from_status"] + " -> " + e["to_status"] if e.get("to_status") else e["kind"]
    print(e["at"][:19], move, e.get("actor"), "|", str(note)[:200].replace("\n", " "))'
}

workers() {
    api GET /workers | python3 -c '
import sys, json
for w in json.load(sys.stdin):
    print(w.get("name"), w.get("state"), "roles", w.get("roles"), "slots", w.get("slots"),
          "ok/fail", w.get("runs_ok"), w.get("runs_failed"), "err", (w.get("last_error") or "")[:120])
    print("   usage", json.dumps(w.get("usage"), ensure_ascii=False)[:300])'
}

summary='import sys, json; t = json.load(sys.stdin); print(t.get("id"), t.get("status"), t.get("detail") or "")'

case "${1:-}" in
    safe) safe ;;
    deploy) deploy "${2:-}" ;;
    tickets) tickets "${2:-10}" ;;
    ticket) ticket "$2" ;;
    workers) workers ;;
    logs) cd "$STACK" && docker compose logs --since "${3:-30m}" "${2:-aico}" 2>&1 | tail -80 ;;
    create)  # create <file.json> (đường dẫn trong WSL, vd /mnt/c/...)
        api POST /tickets "@$2" | python3 -c "$summary" ;;
    decide)  # decide <id> <approve|reject> [ghi chú]
        api POST "/tickets/$2/decisions" \
            "$(python3 -c 'import json,sys; print(json.dumps({"decision": sys.argv[1], "actor": "owner", "comment": sys.argv[2]}))' "$3" "${4:-}")" \
            | python3 -c "$summary" ;;
    escalate)  # escalate <id> <merge|retry_push|...> [ghi chú]
        api POST "/tickets/$2/escalation" \
            "$(python3 -c 'import json,sys; print(json.dumps({"action": sys.argv[1], "actor": "owner", "comment": sys.argv[2]}))' "$3" "${4:-}")" \
            | python3 -c "$summary" ;;
    *)
        echo "lệnh: safe | deploy [--force] | tickets [n] | ticket <id> | workers | logs [service] [since]"
        echo "      create <file.json> | decide <id> <approve|reject> [ghi chú] | escalate <id> <action> [ghi chú]"
        exit 2 ;;
esac
