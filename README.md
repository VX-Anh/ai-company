# AI Company OS (`aico`)

Công ty agent khai báo bằng cấu hình: phòng ban, vai trò, SOP và quyền hạn nằm trong `company/`; LangGraph chạy SOP, mọi tool đi qua guard và MCP server, việc của agent giao cho worker là CLI chính thức của từng subscription (Claude Code, Codex, Cursor), chạy headless trên máy cá nhân. Không dùng proxy biến token subscription thành API. Thiết kế đầy đủ, kế hoạch và sơ đồ ở [`document/`](document/README.md).

## Trạng thái: GĐ0 · Nền móng

| Bước | Nội dung | Trạng thái |
| --- | --- | --- |
| 0.1 | Repo, `uv` workspace, ruff, pyright, pytest; CI trên GitHub Actions (và `.gitlab-ci.yml`) | Xong |
| 0.2 | Model dữ liệu chung (`aico.core`) | Xong |
| 0.3 | Registry nạp và kiểm tra `company/` | Xong |
| 0.4 | Schema Postgres (migration SQL đánh số) | Xong |
| 0.5 | Ticket service, bảng chuyển trạng thái, event | Xong |
| 0.6 | Lease `FOR UPDATE SKIP LOCKED`, nhận lại việc khi tiến trình chết | Xong |
| 0.7 | MCP server `gitlab`: ràng buộc repo, `ai/*`, idempotent, merge cần bản ghi duyệt | Xong (chưa thử với GitLab thật) |
| 0.8 | Guard + gateway + audit | Xong |
| 0.9 | Graph `fix_ticket` với node giả lập, `interrupt()` và resume | Xong |
| 0.10 | Docker Compose cho máy cá nhân | Đã viết, chưa chạy (máy chưa có Docker) |
| 0.10b | Intake polling GitLab | Xong |
| 0.11 | Model relay: token theo task, ngân sách, streaming | Xong (chưa nối 9router thật) |
| 0.11b | Trace sang Langfuse | Mới có trace ra log; chờ chốt Langfuse chạy ở đâu |
| 0.12 | Spike OpenHands SDK | Chưa làm (cần Docker và model) |
| 1.6 (GĐ1) | Tech Lead thật: `LlmAgent` qua model relay, tool `read_file` qua guard; bật bằng `AICO_PLANNER=llm` | Xong (test với 9router giả) |

Cổng ra GĐ0 đã được kiểm bằng test: ticket chạy hết luồng với hai lần duyệt, và khi tiến trình chết ngay sau bước push thì runner khác chạy tiếp từ checkpoint, không tạo commit hay MR trùng (`tests/test_crash_recovery.py`, chạy trên Postgres thật).

## Chạy test (Windows, không cần Docker)

```powershell
uv sync --all-packages
uv run pytest -q          # test Postgres tự dựng Postgres 16 bằng pgserver
uv run ruff check . ; uv run pyright
```

Trên CI, đặt `AICO_TEST_DATABASE_URL` để test dùng Postgres của CI thay cho pgserver.

## Lệnh

```text
aico check-config [company]   kiểm tra cấu hình công ty
aico migrate                  áp migration và tạo bảng checkpoint LangGraph
aico serve                    API (127.0.0.1:8000), model relay (127.0.0.1:8200), runner, intake
mcp-gitlab                    MCP server GitLab (Streamable HTTP, /mcp)
```

Biến môi trường chính: `AICO_PLANNER` (`static` hoặc `llm`), `AICO_DATABASE_URL`, `AICO_COMPANY_DIR`, `AICO_MCP_GITLAB_URL`, `AICO_UPSTREAM_LLM_URL` và `AICO_UPSTREAM_LLM_KEY_FILE` (API trả theo lượng dùng, nếu có), `AICO_API_TOKEN_FILE`, `AICO_INTERNAL_TOKEN_FILE`, `AICO_INTAKE_PROJECTS`, `AICO_SLOW_WORKERS` (mặc định `cursor`: phòng chạy chậm, việc găng như lập kế hoạch, review, đầu việc mà việc khác đang chờ được giao cho phòng khác trước). Secret luôn đọc từ file. Xem `src/aico/settings.py` và `mcp_servers/gitlab/src/mcp_gitlab/server.py`.

## Chạy bằng Docker Compose (sau khi có WSL2 + Docker)

```bash
cd deploy
cp .env.example .env                       # điền mật khẩu, repo pilot
for s in gitlab_token gitlab_merge_token aico_api_token aico_internal_token worker_token; do
  [ -f secrets/$s ] || openssl rand -hex 24 > secrets/$s   # token GitLab thật thì ghi đè
done
touch secrets/remote_ssh_key secrets/remote_known_hosts secrets/cloudflare_tunnel_token  # xem mục dưới
sudo groupadd -g 10100 aico-secrets && sudo usermod -aG aico-secrets $USER
chgrp 10100 secrets/* && chmod 640 secrets/*   # chỉ container aico, mcp-gitlab đọc được
docker compose up -d --build
# Đăng nhập mỗi worker một lần (credential nằm trong volume riêng của worker đó):
docker compose exec -it worker-claude claude          # rồi gõ /login
docker compose exec -it worker-codex codex login --device-auth
docker compose exec -it worker-cursor cursor-agent login
```

`GITLAB_FAKE=1` trong `.env` cho phép chạy thử toàn luồng mà chưa cần GitLab thật.

## Máy build từ xa (server compute)

Service `remote-runner` (`deploy/remote/remote_runner.py`) điều khiển Docker rootless trên server
qua SSH. Server chỉ nhận mã nguồn, không nhận secret nào. Mỗi lần chạy dùng container và volume
tạm: pha setup có mạng (giải nén, `npm ci` / `uv sync` theo lockfile, cache ở volume
`aico-cache`), pha chạy lệnh không có mạng, `--cap-drop ALL`, giới hạn RAM và số tiến trình.

- **Build/test của agent**: trong worker, `aico-remote 'npm run build && npm test'` gửi thư mục
  hiện tại (bỏ `.git`, `node_modules`, `dist`) và trả log cùng mã thoát. Coder và Reviewer được
  nhắc dùng lệnh này khi máy build đã cấu hình. Lệnh chạm file heartbeat để watchdog không coi
  agent đang chờ là treo.
- **Bản xem trước MR**: khi mở MR cho repo trong `AICO_PREVIEW_PROJECTS`, aico tải mã nguồn branch
  qua MCP gitlab, server build (`npm run build -- --base=./`) rồi trả `dist` về PC. Bản xem trước ở
  `http://localhost:8500/preview/<ticket>/`, gắn vào ticket dạng artifact `preview`; dựng lại
  bằng `POST /tickets/{id}/preview`. Danh sách: `http://localhost:8500/preview/`.

Cài đặt: `ssh-keygen -t ed25519 -N "" -f secrets/remote_ssh_key`, thêm khoá công khai vào
`~/.ssh/authorized_keys` của user trên server với tiền tố `restrict`, chép dòng host key của
server từ `known_hosts` đã tin vào `secrets/remote_known_hosts`, đặt `REMOTE_SSH_TARGET` và
`AICO_PREVIEW_PROJECTS` trong `.env`. Để trống `REMOTE_SSH_TARGET` thì agent build/test như cũ.

Sau mỗi lần merge, bản chính của repo (branch đích) được dựng lại với tên theo repo, vd
`aico-office`; dựng tay bằng `POST /tickets/{id}/preview?site=true`.

## Tên miền (Cloudflare Tunnel)

Server ở sau NAT và user không có root, nên không mở cổng: service `cloudflared` (profile
`tunnel`) trên PC nối ra Cloudflare, token chỉ nằm ở `secrets/cloudflare_tunnel_token`.

| Hostname | Service trong tunnel | Nội dung |
| --- | --- | --- |
| `office.aico.io.vn`, path `^/api` | `http://aico:8000` | API (aico phục vụ thêm dưới `/api`, trừ `/internal`) |
| `office.aico.io.vn` | `http://remote-runner:8500` | GUI bản main (`PREVIEW_HOSTS=office.aico.io.vn=aico-office`) |
| `preview.aico.io.vn` | `http://remote-runner:8500` | bản xem trước PR (`PREVIEW_PUBLIC_URL=https://preview.aico.io.vn`) |

Đặt Cloudflare Access (đăng nhập bằng email) cho cả hai hostname trước khi bật tunnel. Qua tên
miền, remote-runner chỉ nhận GET; API vẫn đòi token như khi chạy ở máy này. Trên GUI, nhập địa
chỉ API là `https://office.aico.io.vn/api`.

## MCP server cho agent

| Server | Tool | Ghi chú |
| --- | --- | --- |
| `gitlab` (`mcp-gitlab`) | `read_file`, `list_issues`, `create_branch`, `push`, `open_merge_request`, `get_merge_request`, `merge_mr` | GitHub hoặc GitLab theo `GIT_PROVIDER`; giữ token, tự kiểm tra repo/branch/bản ghi duyệt |
| `pixel` (`mcp-pixel`) | `pixel_help`, `pixel_validate`, `pixel_inspect`, `pixel_patch`, `pixel_render`, `pixel_compile`, `pixel_scene`, `pixel_import` | [Pixelbolt](https://github.com/skulitom/Pixelbolt) v0.9.0 (trước là PixelForge, MIT, offline) sau cầu nối stdio -> HTTP; vẽ pixel art từ công thức JSON, file ra volume `pixel-data` |

Agent chỉ gọi tool qua tool bridge của aico, nên mọi lời gọi đi qua guard (quyền theo `company/roles/*.yaml`, mức theo `company/policies/default.yaml`) và audit. Ảnh trả về (bản xem trước của Pixelbolt) được chuyển tiếp cho worker nhưng không ghi vào audit.

## REST API

| Method | Đường dẫn | Việc |
| --- | --- | --- |
| POST | `/tickets` | Tạo ticket `{title, body, project?, labels?, budget_usd?}` |
| GET | `/tickets`, `/tickets/{id}`, `/tickets/{id}/events` | Xem ticket và lịch sử |
| POST | `/tickets/{id}/decisions` | Duyệt `{decision, actor, comment}`: kế hoạch `approved/revise/rejected`, merge `approved/changes_requested/rejected` |
| POST | `/tickets/{id}/escalation` | Ticket escalated: `{action, actor, comment}`; `close`/`fail` đóng ticket, `merge` cho merge_bot merge MR đã tự kiểm, `retry_push` đẩy lại code chưa đẩy được (lưu trong event escalated) và mở MR |
| GET | `/internal/approvals/{id}?project&mr_iid` | Chỉ cho MCP server, cần token nội bộ |

## Cấu trúc

```text
company/            cấu hình công ty (YAML) và prompt
src/aico/
  core/             model dữ liệu và spec, không phụ thuộc package khác
  registry/         nạp + kiểm tra company/
  db/               pool Postgres, migration SQL
  tickets/          state machine, store in-memory và Postgres (lease)
  governance/       bản ghi duyệt, audit log, xác nhận duyệt merge
  tools/            guard, gateway, MCP client
  orchestrator/     graph fix_ticket, cổng agent (bản giả lập GĐ0), runner
  agents/           LlmAgent (create_agent qua model relay), Tech Lead thật
  llm_relay/        model relay trước 9router: token task, ngân sách
  intake/           polling GitLab lấy ticket
  api/              REST API
  observability/    trace model
mcp_servers/gitlab/ MCP server GitLab (package riêng trong workspace)
deploy/             Dockerfile, compose.yaml, .env.example
tests/              test đơn vị, test luồng, test Postgres
```

## Khác với tài liệu thiết kế

- Node chờ duyệt không tự chuyển ticket sang `awaiting_*`; runner làm việc đó sau khi checkpoint có `interrupt()` đã lưu. Nếu node tự chuyển, tiến trình chết đúng lúc đó sẽ để ticket ở trạng thái chờ mà không có điểm dừng để resume.
- Migration dùng file SQL đánh số trong `src/aico/db/migrations/` thay cho Alembic.
- Merge do vai trò hệ thống `merge_bot` (không gọi model); intake dùng vai trò hệ thống `intake`.
