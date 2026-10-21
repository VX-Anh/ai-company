# Ticket: worker báo hoạt động chi tiết của agent

Trạng thái: **đã làm** (21/10/2026, Claude Code làm tay). Repo: `VX-Anh/ai-company` (aico không tự sửa repo này).

Kết quả kiểm tra: cả 3 CLI đều ghi transcript trong home của worker, nên chưa cần nguồn dự phòng (stdout dạng
stream). Codex bỏ `--ephemeral` để có rollout; rollout cũ hơn 7 ngày bị xoá. Rollout chỉ ghi item khi đã xong,
nên hoạt động của Codex chậm một bước. Code: `deploy/worker/activity.py`, test: `tests/test_worker_activity.py`.
Người dùng cần: aico-office hiện được agent đang làm gì, theo kiểu Habblaud
(xem `aico-office/docs/references/habblaud.md`).

## Mục tiêu

`GET /workers` (và sau này stream) có thêm, cho mỗi agent con đang chạy: hoạt động hiện tại, vài hoạt động gần nhất,
danh sách task, lệnh đang chờ và số liệu. Nguồn ưu tiên là **transcript** mà CLI tự ghi; CLI nào không có
transcript thì đọc **output dạng stream của CLI**.

## Nguồn theo từng CLI

| CLI | Nguồn chính | Dự phòng |
| --- | --- | --- |
| Claude Code | Transcript `~/.claude/projects/<cwd đã mã hoá>/<session>.jsonl`, ghi dần trong lúc chạy; subagent ở `<session>/subagents/**/agent-*.jsonl` | `--output-format stream-json --verbose` (stdout, mỗi dòng một event) |
| Codex | Rollout `~/.codex/sessions/**/rollout-*.jsonl`. Hiện worker chạy `codex exec --ephemeral`, có thể không ghi rollout: kiểm trước khi bỏ `--ephemeral` | `codex exec --json` (JSONL event trên stdout) |
| Cursor | Chưa thấy transcript có tài liệu: kiểm `~/.cursor` trong container | `cursor-agent --output-format stream-json` |

Đổi `--output-format json` sang dạng stream thì `parse_output` và `token_usage` phải lấy kết quả cuối từ event
`result` cuối cùng; giữ test hiện có của hai hàm này.

## Thiết kế

1. **`deploy/worker/activity.py`**, phần thuần, có test. Nhận từng dòng JSONL của transcript hoặc stream và trả về
   `Activity {kind, icon, text, detail?, tool?, at, error?}`.
   - `kind` dùng chung bộ với Habblaud: `prompt think respond read search edit write run test git web mcp plan
     delegate ask wait error done other`.
   - Map theo tên tool:
     - `Read`, `Glob`, `Grep` → `read`/`search`; `Edit`, `Write` → `edit`/`write`; `Bash` → `run`, hoặc `test` khi
       lệnh chứa test/pytest/vitest, `git` khi lệnh là git;
     - `WebFetch` → `web`; `mcp__aico__*` → `mcp` (tên tool aico đưa vào `text`); `TodoWrite` → `plan`, kèm danh
       sách task; `Agent`/`Task` → `delegate`;
     - lệnh `aico-remote` → `run`, `text` là "Build/test trên server".
   - `text` tiếng Việt, tối đa khoảng 46 ký tự, ví dụ "Sửa src/api.ts". `detail` tối đa 300 ký tự.
   - **Che secret trước khi lưu:** `Bearer …`, `TOKEN=…`, `sk-…`, `ghp_…`, `github_pat_…`, JWT, mật khẩu trong URL,
     và giá trị của `AICO_TASK_TOKEN` cùng token MCP của job (biết trước nên thay chính xác).
2. **Theo dõi trong `run_agent`.** Một luồng phụ tail transcript của job, hoặc đọc stdout theo dòng khi dùng dự
   phòng. Mỗi dòng mới cập nhật `WorkerStats.agents[agent_id]`:
   - `activity`;
   - `recent` (tối đa 20);
   - `tasks` (từ `TodoWrite`/plan);
   - `shells` (lệnh nền chưa xong);
   - `stats` (số lần gọi tool, token vào/ra, số dòng thêm/xoá nếu có).

   Hoạt động mới cũng chạm heartbeat, để watchdog có thêm tín hiệu sống. Tìm transcript: file `.jsonl` mới nhất trong
   thư mục project ứng với `cwd` của job, được tạo sau lúc job bắt đầu.
3. **API.** `snapshot()` trả các trường mới trong `agents[]` (chỉ thêm, không đổi trường cũ). aico chuyển nguyên
   qua `/workers`. Chưa cần SSE ở bước này.
4. **Không giữ hội thoại.** Chỉ giữ tóm tắt hoạt động; transcript gốc vẫn nằm trong volume của worker như hiện nay.
   Không thêm endpoint xem toàn bộ hội thoại ở ticket này.

## Kiểm tra

- Test thuần cho `activity.py`: chạy với các dòng mẫu của cả 3 CLI, gồm transcript Claude, rollout hoặc `--json`
  của Codex, stream-json của Cursor. Lấy dòng thật từ một lần chạy rồi bỏ nội dung nhạy cảm.
- Test che secret: token của job không bao giờ xuất hiện trong `snapshot()`.
- Test `parse_output`/`token_usage` với output dạng stream.
- Chạy thật một ticket nhỏ, xem `GET /workers` có `activity` đổi theo từng bước.
- `uv run pytest -q`, ruff và pyright sạch.

## Sau ticket này

Ticket cho aico-office: bong bóng hoạt động, màn hình trên bàn đổi theo `kind`, tiến độ task, đồng hồ cát khi chờ
lệnh, và ngăn chi tiết agent hiện `recent`/`stats`.
