# AI Company OS (`aico`)

Công ty agent khai báo bằng cấu hình: phòng ban, vai trò, SOP và quyền hạn nằm trong `company/`; LangGraph chạy SOP, mọi tool đi qua guard và MCP server, mọi lời gọi model đi qua model relay tới 9router. Thiết kế đầy đủ, kế hoạch và sơ đồ ở [`document/`](document/README.md).

## Trạng thái: GĐ0 · Nền móng

| Bước | Nội dung | Trạng thái |
| --- | --- | --- |
| 0.1 | Repo, `uv` workspace, ruff, pyright, pytest, `.gitlab-ci.yml` | Xong |
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

Biến môi trường chính: `AICO_DATABASE_URL`, `AICO_COMPANY_DIR`, `AICO_MCP_GITLAB_URL`, `AICO_UPSTREAM_LLM_URL` (9router), `AICO_UPSTREAM_LLM_KEY_FILE`, `AICO_API_TOKEN_FILE`, `AICO_INTERNAL_TOKEN_FILE`, `AICO_INTAKE_PROJECTS`. Secret luôn đọc từ file. Xem `src/aico/settings.py` và `mcp_servers/gitlab/src/mcp_gitlab/server.py`.

## Chạy bằng Docker Compose (sau khi có WSL2 + Docker)

```bash
cd deploy
cp .env.example .env                       # điền mật khẩu, repo pilot
for s in gitlab_token gitlab_merge_token ninerouter_key aico_api_token aico_internal_token; do
  [ -f secrets/$s ] || openssl rand -hex 24 > secrets/$s   # token GitLab thật thì ghi đè
done
docker compose up -d --build
```

`GITLAB_FAKE=1` trong `.env` cho phép chạy thử toàn luồng mà chưa cần GitLab thật. Dashboard 9router ở `http://127.0.0.1:20128` (đổi mật khẩu mặc định, tạo API key rồi ghi vào `secrets/ninerouter_key`).

## REST API

| Method | Đường dẫn | Việc |
| --- | --- | --- |
| POST | `/tickets` | Tạo ticket `{title, body, project?, labels?, budget_usd?}` |
| GET | `/tickets`, `/tickets/{id}`, `/tickets/{id}/events` | Xem ticket và lịch sử |
| POST | `/tickets/{id}/decisions` | Duyệt `{decision, actor, comment}`: kế hoạch `approved/revise/rejected`, merge `approved/changes_requested/rejected` |
| POST | `/tickets/{id}/escalation` | Ticket escalated: `{action: close/fail, actor}` |
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
