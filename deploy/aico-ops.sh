#!/bin/bash
# Lệnh vận hành stack aico trong WSL (user aico). Gọi từ Windows:
#   wsl -d Ubuntu -u aico -- bash /mnt/d/TVPL/Marshy/ai-company/deploy/aico-ops.sh <lệnh> [tham số]
# Token API đọc từ secrets/, không in ra.
set -u
SRC="${AICO_SRC:-/mnt/d/TVPL/Marshy/ai-company}"
STACK="${AICO_STACK:-$HOME/ai-company/deploy}"
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
          if t["status"] in ("new", "triaged", "planned", "in_progress", "in_review", "in_testing", "merging")
          or t.get("pending_resume")))')
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

# Mốc deploy cho canary (event job ghi company_sha): git sha 11 ký tự của bản nguồn; khi có sửa
# chưa commit hoặc file chưa theo dõi thì thêm "x" + 8 ký tự băm của phần đó, để hai lần deploy bẩn
# khác nhau trên cùng commit ra hai mốc khác nhau ("x" không phải chữ số hex: canary tách được
# sha và băm). Prompt nằm cả trong code, nên đổi code cũng mở đợt canary mới.
deploy_mark() {
    local g=(git -c safe.directory='*' -C "$SRC") sha dirty
    sha="$("${g[@]}" rev-parse --short=11 HEAD 2>/dev/null)"
    [ -n "$sha" ] || return 0
    if [ -n "$("${g[@]}" status --porcelain 2>/dev/null | head -c 1)" ]; then
        # Băm: diff của file đã theo dõi + danh sách trạng thái + nội dung file chưa theo dõi
        # (status chỉ nêu tên, sửa tiếp một file chưa theo dõi vẫn phải đổi mốc).
        dirty="$({
            "${g[@]}" diff --binary HEAD
            "${g[@]}" status --porcelain
            "${g[@]}" ls-files --others --exclude-standard -z | (cd "$SRC" && xargs -0 -r sha1sum)
        } 2>/dev/null | sha256sum | cut -c1-8)"
        sha="${sha}x${dirty}"
    fi
    printf '%s
' "$sha"
}

# Ghi mốc vào dòng AICO_COMPANY_SHA của $STACK/.env (compose tự đọc file này), không chỉ export
# trong shell của deploy: lần `docker compose up` gõ tay sau đó vẫn dùng đúng mốc của bản code đang
# chạy. Mốc rỗng (không đọc được git) vẫn ghi để xoá mốc cũ. Không in nội dung .env.
write_mark() {  # write_mark <mốc>
    local env="$STACK/.env" tmp
    tmp="$(mktemp "$STACK/.env.XXXXXX")" || return 1
    if [ -f "$env" ]; then
        chmod --reference="$env" "$tmp"
        grep -v '^AICO_COMPANY_SHA=' "$env" >"$tmp"
    fi
    printf 'AICO_COMPANY_SHA=%s
' "$1" >>"$tmp"
    mv "$tmp" "$env"
}

canary_query() {  # canary_query [sha|-] [n]: query string đã URL-encode ("-" hoặc trống = mới nhất)
    python3 -c '
import sys, urllib.parse
sha, n = sys.argv[1], sys.argv[2]
params = {"n": n}
if sha not in ("", "-"):
    params["sha"] = sha
print(urllib.parse.urlencode(params))' "${1:-}" "${2:-10}"
}

deploy() {
    if [ "${1:-}" != "--force" ] && ! safe; then
        echo "Đang có job chạy; đợi xong hoặc dùng: deploy --force" >&2
        return 1
    fi
    # Không dùng git checkout/pull trong bản sao WSL: nó từng đưa file về commit cũ.
    (cd "$SRC" && tar --exclude=./.git --exclude=./.venv --exclude=./.pytest_cache \
        --exclude=./.ruff_cache --exclude=./.ssh --exclude=./.claude --exclude=./deploy/secrets \
        --exclude=./deploy/.env --exclude='__pycache__' -cf - .) | tar -xf - -C "$(dirname "$STACK")"
    cd "$STACK" || return 1
    write_mark "$(deploy_mark)" || return 1
    # Biến trong shell thắng .env của compose: bỏ đi để mốc chỉ có một nguồn là .env.
    unset AICO_COMPANY_SHA
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

ticket() {  # ticket <id>: trạng thái, artifact và các event gần nhất kèm ghi chú (NOTE_CHARS=0: đủ)
    api GET "/tickets/$1" | python3 -c '
import sys, json
t = json.load(sys.stdin)
print(t["status"], "|", t["title"])
for a in t.get("artifacts", []):
    print("  artifact", a["kind"], a["ref"], a.get("url") or "")'
    api GET "/tickets/$1/events" | python3 -c '
import os, sys, json
width = int(os.environ.get("NOTE_CHARS", "200"))  # NOTE_CHARS=0: in đủ ghi chú
for e in json.load(sys.stdin)[-12:]:
    d = e.get("data") or {}
    note = d.get("review_notes") or d.get("reason") or d.get("note") or d.get("summary") or ""
    move = str(e.get("from_status") or "-") + " -> " + e["to_status"] if e.get("to_status") else e["kind"]
    print(e["at"][:19], move, e.get("actor"), "|", (str(note)[:width] if width else str(note)).replace("\n", " "))'
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

# Được `source` (test): chỉ nạp hàm, không chạy lệnh.
if [ "${BASH_SOURCE[0]}" != "$0" ]; then
    return 0
fi

case "${1:-}" in
    safe) safe ;;
    mark)  # mốc deploy của bản nguồn lúc này và mốc đang ghi trong .env (chỉ in dòng đó)
        echo "nguồn: $(deploy_mark)"
        echo ".env:  $(grep -s '^AICO_COMPANY_SHA=' "$STACK/.env" | tail -1 | cut -d= -f2-)" ;;
    deploy) deploy "${2:-}" ;;
    tickets) tickets "${2:-10}" ;;
    ticket) ticket "$2" ;;
    workers) workers ;;
    logs) cd "$STACK" && docker compose logs --since "${3:-30m}" "${2:-aico}" 2>&1 | tail -80 ;;
    create)  # create <file.json> (đường dẫn trong WSL, vd /mnt/c/...)
        api POST /tickets "@$2" | python3 -c "$summary" ;;
    decide)  # decide <id> <approved|revise|changes_requested|rejected> [ghi chú]
        api POST "/tickets/$2/decisions" \
            "$(python3 -c 'import json,sys; print(json.dumps({"decision": sys.argv[1], "actor": "owner", "comment": sys.argv[2]}))' "$3" "${4:-}")" \
            | python3 -c "$summary" ;;
    metrics)  # metrics [số ngày]: chỉ số hiệu năng hệ agent (Phòng R&D), chỉ đọc
        api GET "/rnd/metrics?days=${2:-30}" | python3 -c '
import sys, json
d = json.load(sys.stdin)
a = d["all"]
print("{} ngày: {tickets} ticket, xong {done}, escalate {escalated}, lỗi {failed}".format(d["days"], **a))
print("đạt ngay vòng đầu {first_pass_rate}, vòng review TB {avg_review_rounds}, người trả lại {owner_return_rate}, phút tới chờ merge TB {avg_minutes_to_merge_approval}".format(**a))
print("escalate:", a["escalations"])
print("job: {jobs}, ok {ok_rate}, hết hạn mức {limited}, token {tokens}, lỗi {errors}".format(**a["jobs"]), "| có <knowledge>:", a["knowledge_jobs"])
for group in ("jobs_by_role", "jobs_by_room"):
    print(group + ":")
    for k, v in a[group].items():
        print("  {}: {jobs} job, ok {ok_rate}, trung vị {median_seconds}s, token TB {avg_tokens}".format(k, **v))
k = d["knowledge_effect"]
print("chèn tri thức: có {} ticket (đạt ngay {}, review TB {}) | không {} ticket (đạt ngay {}, review TB {})".format(
    k["with_knowledge"]["tickets"], k["with_knowledge"]["first_pass_rate"], k["with_knowledge"]["avg_review_rounds"],
    k["without"]["tickets"], k["without"]["first_pass_rate"], k["without"]["avg_review_rounds"]))
s = d.get("diff_size") or {}
if s.get("measured"):
    print("theo cỡ diff ở lần review đầu ({} ticket có số đo, trung vị {} dòng; tương quan, không phải nhân quả):".format(s["measured"], s["median_diff_lines"]))
    cells = list(s["by_bucket"].items()) + [("<= trung vị", s["at_or_below_median"]), ("> trung vị", s["above_median"])]
    for k, v in cells:
        if v["tickets"]:
            print("  {} dòng: {tickets} ticket, đạt ngay {first_pass_rate}, review TB {avg_review_rounds}, TB {avg_diff_lines} dòng / {avg_diff_files} file".format(k, **v))
print("theo nhãn:")
for k, v in d["by_label"].items():
    print("  {}: {tickets} ticket, đạt ngay {first_pass_rate}, review TB {avg_review_rounds}, escalate {escalated}".format(k, **v))
' ;;
    canary)  # canary [sha|-] [n]: n ticket đầu của cấu hình sha (bỏ trống hoặc "-": mới nhất) so với n ticket bắt đầu ngay trước; chỉ đọc
        api GET "/rnd/canary?$(canary_query "${2:-}" "${3:-10}")" | python3 -c '
import sys, json
d = json.load(sys.stdin)
if "after" not in d:
    sys.exit("lỗi: " + json.dumps(d, ensure_ascii=False))
print("canary {} (n={}, {} ngày)".format(d["sha"], d["n"], d["days"]))
for name, key in (("sau  ", "after"), ("trước", "before")):
    g = d[key]
    print("  {}: {tickets} ticket, đã kết thúc {finished} (xong {done}, escalate {escalated}, lỗi {failed}; không-done {non_done_rate}), đạt vòng đầu {first_pass_rate}, vòng review TB {avg_review_rounds}, người trả lại {owner_return_rate}, token TB {avg_tokens}".format(name, **g))
    print("         ticket:", " ".join(g["ids"]) or "-", "| cấu hình:", g["company_shas"])
    if g["pending"]:
        print("         chưa kết thúc:", " ".join(g["pending"]))
m = d.get("mixed") or {}
if m.get("tickets"):
    print("  loại {} ticket vắt qua lần deploy (job mang hơn một company_sha): {}".format(m["tickets"], " ".join(m["ids"])))
verdict = {True: "XẤU ĐI: xem lại thay đổi, cân nhắc revert (không tự revert)", False: "không xấu đi quá ngưỡng", None: "chưa kết luận"}[d["regressed"]]
print("kết quả:", verdict)
for r in d["reasons"]:
    print("  -", r)
print("ghi chú:", d["note"])
' ;;
    bench)  # bench [số ngày]: ứng viên bộ ticket chuẩn (JSON), chọn 10–15 lưu vào eval/tickets/
        api GET "/rnd/bench?days=${2:-90}" | python3 -m json.tool --no-ensure-ascii ;;
    scan)  # scan [số ngày]: tạo ticket research dò công nghệ theo company/rnd/watchlist.yaml
        body="$(api GET "/rnd/scan-ticket?days=${2:-7}")"
        api POST /tickets "$body" | python3 -c "$summary" ;;
    eval-start)  # eval-start <biến thể> [số lần] < suite.json: tạo ticket chạy lại (instance aico-eval chạy)
        cd "$STACK" && docker compose exec -T aico aico eval start --variant "$2" --suite - --repeat "${3:-2}" ;;
    eval-report)  # eval-report <biến thể> [--close] [< suite.json]: kết quả; --close đóng ticket đã chấm
        cd "$STACK" && if [ -t 0 ]; then
            docker compose exec -T aico aico eval report --variant "$2" ${3:-}
        else
            docker compose exec -T aico aico eval report --variant "$2" --suite - ${3:-}
        fi ;;
    eval-grade)  # eval-grade <biến thể> [--regrade] < suite.json: chấm acceptance {build, test} trên máy build
        cd "$STACK" && docker compose exec -T aico aico eval grade --variant "$2" --suite - ${3:-} ;;
    eval-cleanup)  # eval-cleanup <biến thể> [--dry-run]: xoá nhánh ai/<id>, base/<id> của ticket chạy lại đã đóng
        cd "$STACK" && docker compose exec -T aico aico eval cleanup --variant "$2" ${3:-} ;;
    remote-image)  # dựng image build mặc định (Node + thư viện Chromium) trên server
        cd "$STACK" && docker compose exec -T remote-runner             python3 /opt/remote_runner.py build-image aico/node-playwright:22             < "$STACK/remote/node-playwright.Dockerfile" ;;
    escalate)  # escalate <id> <merge|retry_push|...> [ghi chú]
        api POST "/tickets/$2/escalation" \
            "$(python3 -c 'import json,sys; print(json.dumps({"action": sys.argv[1], "actor": "owner", "comment": sys.argv[2]}))' "$3" "${4:-}")" \
            | python3 -c "$summary" ;;
    *)
        echo "lệnh: safe | deploy [--force] | tickets [n] | ticket <id> | workers | logs [service] [since] | metrics [ngày] | canary [sha|-] [n] | mark | bench [ngày] | scan [ngày]"
        echo "      eval-start <biến thể> [lần] < suite.json | eval-report <biến thể> [--close] | eval-grade <biến thể> < suite.json | eval-cleanup <biến thể> [--dry-run] (cần: docker compose --profile eval up -d aico-eval)"
        echo "      create <file.json> | decide <id> <approved|revise|changes_requested|rejected> [ghi chú] | escalate <id> <action> [ghi chú]"
        echo "      remote-image (dựng image Node + Chromium trên máy build, chạy lại khi sửa node-playwright.Dockerfile)"
        exit 2 ;;
esac
