# AI Company OS — Kế hoạch triển khai từng bước

Cập nhật: 2026-10-19 · Dựa trên [thiết kế kiến trúc](thiet-ke-kien-truc.md) và [lộ trình 4 giai đoạn](so-do/lo-trinh-trien-khai.html)

Mục tiêu cuối: một phòng Kỹ thuật gồm các agent nhận ticket thật, sửa code trong sandbox, mở MR và merge sau khi người duyệt, rồi mở rộng sang phòng ban khác chỉ bằng cấu hình. Kế hoạch đi theo thứ tự phụ thuộc: mỗi bước dùng được kết quả của bước trước, và mỗi giai đoạn chỉ mở khi qua cổng ra.

Thời lượng là ước lượng cho một team nhỏ; ngưỡng ở các cổng ra (tỉ lệ merge, chi phí) là việc team phải chốt trước khi bắt đầu giai đoạn đó.

Môi trường: chủ yếu một máy cá nhân Windows chạy WSL2 và Docker Compose; cổng model là 9router chạy trên máy, đứng sau model relay của `aico`; một số thành phần (Langfuse, Qdrant, GitLab Runner) có thể đặt trên server Linux. Không dùng Kubernetes.

## Tổng quan

| Giai đoạn | Thời lượng | Kết quả | Cổng ra |
| --- | --- | --- | --- |
| Bước 0 · Chuẩn bị | 1 tuần | Máy đã dựng, quyết định đã chốt, PoC sandbox Docker | Có câu trả lời cho mọi mục "Cần chốt" |
| GĐ0 · Nền móng | ~2 tuần | Khung chạy được trên máy cá nhân | 1 ticket giả chạy hết luồng, sống sót khi kill `aico` |
| GĐ1 · Một nhân viên | 3–4 tuần | Coder thật sửa ticket thật trên repo pilot | Tỉ lệ MR được merge đạt ngưỡng đã chốt |
| GĐ2 · Một phòng ban | 4–6 tuần | Đủ phòng Kỹ thuật, chạy hằng ngày trên máy cá nhân | Ổn định trên repo thật, chi phí trong hạn mức |
| GĐ3 · Mở rộng | liên tục | Phòng Pháp lý, Vận hành, MCP server mới | Mỗi phòng mới có eval riêng trước khi bật |

## Bước 0 · Chuẩn bị (tuần 0)

Dựng máy và chốt các quyết định đang mở trước khi viết code, vì chúng đổi nơi chạy, dữ liệu được phép đụng và cách chạy sandbox.

0. **Dựng máy cá nhân.**
    - Bật WSL2, cài Ubuntu, cài Docker Engine trong WSL2 ở chế độ rootless.
    - Đặt `memory` và `processors` trong `%UserProfile%\.wslconfig`.
    - Cài `uv`, Python 3.12, git trong WSL; clone mọi repo vào `~/...`, không dùng `/mnt/c`.
1. **Chốt phạm vi và chủ sở hữu.** Dự án phục vụ TVPL hay riêng; ai là Board (người duyệt L3), ai là trưởng nhóm duyệt ticket `high`.
2. **Chốt nguồn ticket.** GitLab Issues hay Jira; máy cá nhân lấy ticket bằng polling API, chưa cần webhook.
3. **Chọn repo pilot.** Repo có test tự động tốt, CI ổn định, có sẵn 20–30 issue đã đóng kèm bản sửa để làm eval.
4. **Dựng 9router và chọn model cho từng vai trò.** Chạy 9router bằng container, bật API key cho các route `/v1/*`, đặt hạn mức theo provider. Khai báo model hoặc combo cho từng vai trò, chuỗi fallback có ít nhất một provider dùng API key.
5. **PoC sandbox OpenHands bằng Docker** (việc rủi ro nhất, làm song song 1–7).
    - Chạy OpenHands SDK với workspace Docker trên Docker rootless, bằng các cờ trong mục Bảo mật: non-root, `--cap-drop ALL`, `--read-only`, giới hạn tài nguyên.
    - Kiểm tra agent vẫn sửa code và chạy test được.
6. **Chốt chỗ đặt từng thành phần.** Langfuse, Qdrant đã có trên server chưa, hay dùng Langfuse Cloud; máy cá nhân có bao nhiêu RAM, có GPU không; từ đó chốt số sandbox chạy cùng lúc.
7. **Chốt ngưỡng cổng ra GĐ1.** Ví dụ: tỉ lệ MR được merge trên bộ eval, chi phí trung bình mỗi ticket.

**Xong khi:** máy cá nhân chạy được `docker compose` trong WSL2; mọi mục "Cần chốt" trong thiết kế có câu trả lời ghi lại; PoC sandbox chạy được với đủ cờ bảo mật.

## GĐ0 · Nền móng (~2 tuần)

Tiến độ thực tế ghi ở [README của repo](../README.md) (bảng Trạng thái).

Dựng khung chạy được toàn bộ luồng với các bước giả lập, để GĐ1 chỉ việc thay bước giả bằng agent thật.

| # | Việc | Đầu ra | Xong khi |
| --- | --- | --- | --- |
| 0.1 | Tạo repo `ai-company`, `uv` workspace, ruff, pyright, pytest; GitLab CI chạy lint và test | Repo + `.gitlab-ci.yml` | MR đầu tiên có CI xanh |
| 0.2 | Model dữ liệu chung trong `core/`: `Ticket`, `Task`, `Artifact`, `Event`, `RoleSpec`, `SopSpec` (Pydantic) | `src/aico/core/` | Unit test validate được YAML mẫu |
| 0.3 | Registry nạp `company/`, validate, cấu hình sai thì từ chối khởi động | `src/aico/registry/` + `company/` mẫu (coder, tech_lead) | Test: YAML sai làm app không khởi động |
| 0.4 | Schema Postgres bằng migration: `tickets` (có `lease_owner`, `lease_until`), `ticket_events`, `approvals`, `budget_ledger`; bảng checkpoint qua `saver.setup()` | Migration SQL đánh số (`src/aico/db/migrations/`) | Migrate lên DB trống chạy được, chạy lại không lỗi |
| 0.5 | Ticket service với bảng chuyển trạng thái; mỗi lần chuyển ghi một event | `src/aico/tickets/` + REST `POST/GET /tickets` | Test: chuyển trạng thái sai bị từ chối |
| 0.6 | Lease: nhận ticket bằng `SELECT ... FOR UPDATE SKIP LOCKED`, gia hạn định kỳ, nhả khi xong; scheduler nhận lại ticket hết lease khi `aico` khởi động | Vòng worker + `scheduler/` | Test: kill `aico` rồi chạy lại, ticket tiếp tục; 2 tiến trình không cùng xử lý một ticket |
| 0.7 | MCP server `gitlab` (FastMCP): `read_file`, `create_branch`, `push`, `open_merge_request`; kiểm tra `constraints` (repo, `ai/*`); idempotent | `mcp_servers/gitlab/` | Test: push sang branch ngoài `ai/*` bị từ chối; gọi lại không tạo MR trùng |
| 0.8 | Guard + MCP client: nạp tool theo `tools:` của vai trò, kiểm quyền, mức L0–L3, ghi audit | `src/aico/tools/` | Test: vai trò không khai báo tool thì không thấy tool; L3 thành yêu cầu duyệt |
| 0.9 | Graph `fix_ticket` tối thiểu với node giả lập: triage theo luật, plan cố định, `approve_plan` có `interrupt()`, code áp một patch có sẵn, review luôn đạt, `approve_merge`, merge giả | `src/aico/orchestrator/` | Ticket đi hết luồng, resume bằng `Command(resume=...)` qua REST |
| 0.10 | Docker Compose trên máy cá nhân: Postgres (có volume), MCP `gitlab`, `aico`; mạng nội bộ không publish cổng, API chỉ bind `127.0.0.1`; 9router trong cùng stack; trỏ Langfuse (server hoặc Cloud) | `deploy/compose.yaml` | Một lệnh `docker compose up` dựng được môi trường |
| 0.10b | Intake: polling GitLab/Jira lấy ticket mới theo nhãn, chống tạo trùng | `src/aico/intake/` | Issue gắn nhãn tạo đúng một ticket |
| 0.11 | Model relay bản đầu: endpoint `/v1/chat/completions` (có streaming) chuyển tới 9router, token tạm theo task, ghi `usage` theo ticket | `src/aico/llm_relay/` | `LlmAgent` gọi model qua relay; mỗi request ghi được token theo ticket |
| 0.11b | Observability: Langfuse callback với `session_id` = ticket id cho graph; relay gửi trace model | `src/aico/observability/` | Trace workflow và trace model của một ticket xem được cùng nhau |
| 0.12 | Spike OpenHands SDK trỏ về model relay → 9router, chạy một task trên repo thử bằng workspace Docker (nối tiếp PoC Bước 0) | Ghi chú spike + script | Agent sửa được một bug nhỏ và trả diff |

**Cổng ra GĐ0:** một ticket giả chạy hết luồng trên máy cá nhân, gồm hai lần duyệt qua REST; kill tiến trình `aico` giữa chừng (hoặc cho máy ngủ) thì ticket vẫn chạy tiếp từ checkpoint, không tạo branch hay MR trùng.

## GĐ1 · Một nhân viên (3–4 tuần)

Thay bước giả bằng Coder thật trên repo pilot; review vẫn do người làm (đúng bản rút gọn trong thiết kế).

| # | Việc | Đầu ra | Xong khi |
| --- | --- | --- | --- |
| 1.1 | `sandbox_launcher`: dịch vụ giữ quyền Docker rootless, API tạo sandbox theo mẫu cố định (image, cờ bảo mật, mạng), lấy kết quả, xóa; giới hạn số sandbox cùng lúc | `sandbox_launcher/` + image sandbox | Test: không tạo được container với tham số ngoài mẫu; `aico` không thấy Docker socket |
| 1.2 | Mạng sandbox: mạng Docker `internal: true` chỉ có model relay và egress proxy; proxy lọc theo `allowlist.txt` (mirror registry) | Cấu hình Compose + proxy | Test từ trong sandbox: không gọi được 9router, GitLab, mạng LAN, Internet |
| 1.3 | `OpenHandsWorker`: lấy repo qua MCP `gitlab`, nhờ launcher chép vào volume workspace, chạy agent, lấy diff và log, xóa sandbox | `agents/openhands_worker.py` | Chạy 1 task thật, sandbox không giữ credential nào |
| 1.4 | Ngân sách trong model relay: kiểm model theo vai trò, giữ chỗ trước mỗi lời gọi, đối soát bằng `usage`, từ chối khi hết trần; bảng giá trong `policies/` | `llm_relay/` + `governance/budget` | Test: chạm trần thì relay từ chối, ticket dừng và hỏi người; request đồng thời không vượt trần |
| 1.5 | Bước publish: `aico` push `ai/*` và mở MR qua guard; đối soát MR đã có trước khi tạo | Node `code` hoàn chỉnh | Chạy lại node không tạo MR trùng |
| 1.6 | Tech Lead `LlmAgent` lập kế hoạch bằng `create_agent` với tool gitlab đọc | `agents/llm_agent.py` + prompt | Kế hoạch nêu được bước và file cần sửa |
| 1.7 | Triage theo luật (từ khóa, nhãn, đường dẫn file) gán phòng ban và rủi ro ticket | Node `triage` | Ticket mẫu được gán đúng `low`/`medium`/`high` |
| 1.8 | Duyệt kế hoạch và duyệt merge qua REST (chưa có console); ghi bản ghi duyệt; hạn chờ duyệt | `governance/approvals` | Quá hạn thì ticket `escalated` |
| 1.9 | Bộ eval: 20–30 ticket từ issue đã đóng của repo pilot; script chấm test pass, độ giống bản sửa, chi phí, thời gian | `evals/` | Chạy được một lệnh, ra bảng điểm |
| 1.10 | Chạy pilot: eval trước, rồi ticket thật nhỏ có test; người review và merge tay | Báo cáo pilot | Đủ số ticket để đo tỉ lệ merge |

**Cổng ra GĐ1:** tỉ lệ MR được merge (trên eval và ticket thật) đạt ngưỡng đã chốt ở Bước 0; không có sự cố bảo mật từ sandbox.

## GĐ2 · Một phòng ban (4–6 tuần)

Thêm Reviewer, QA, Chief of Staff và các cơ chế vận hành để chạy liên tục trên môi trường thật.

| # | Việc | Đầu ra | Xong khi |
| --- | --- | --- | --- |
| 2.1 | Reviewer `LlmAgent` đọc diff và kết quả CI, comment MR | Node `review` thật | Review bắt được lỗi trong bộ eval có lỗi cố ý |
| 2.2 | QA chạy test trong sandbox (L1) | Vai trò `qa` | Kết quả test đính kèm MR |
| 2.3 | Chief of Staff định tuyến bằng LLM thay luật | Node `triage` thật | Định tuyến đúng trên bộ ticket mẫu |
| 2.4 | Vòng review có trần: `review_rounds`, `merge_attempts`, escalate khi quá | Graph đầy đủ như thiết kế | Test từng nhánh trong bảng chuyển trạng thái |
| 2.5 | Merge bot và tool `merge_mr` đòi bản ghi duyệt hợp lệ; protected branch cấu hình như thiết kế | MCP `gitlab` bản đầy đủ | Merge không có bản ghi duyệt bị từ chối |
| 2.6 | Console tối giản: hàng chờ duyệt, danh sách ticket, chi phí, kill switch | `console/` | Người duyệt làm hết việc duyệt trên console |
| 2.7 | Kill switch theo agent, phòng ban, toàn công ty | `governance/` | Bật kill switch thì ticket đang chạy dừng ở checkpoint |
| 2.8 | Metrics Prometheus và dashboard: ticket theo trạng thái, tỉ lệ merge, chi phí mỗi ticket, thời gian chờ duyệt | `observability/` + dashboard | Dashboard có số liệu thật |
| 2.9 | Vận hành trên máy cá nhân: image build bằng GitLab CI (runner trên server) `lint → test → eval smoke → build image`; cập nhật bằng `docker compose pull && up -d`; secret qua Docker secrets; `pg_dump` định kỳ ra server | `deploy/compose.yaml`, `deploy/compose.server.yaml`, script backup | Khôi phục được Postgres từ bản backup; cập nhật không mất ticket đang chạy |
| 2.10 | Kiểm thử bảo mật: thử thoát sandbox ra WSL2, gọi Docker từ sandbox, prompt injection trong ticket, vượt ngân sách bằng request đồng thời | Báo cáo kiểm thử | Không còn lỗi mức nghiêm trọng |

**Cổng ra GĐ2:** chạy ổn định trên repo thật trong khoảng thời gian team chốt; chi phí nằm trong hạn mức; không ticket nào kẹt mà không escalate.

**Khi nào chuyển lên server:** khi cần chạy 24/7, nhận webhook, hoặc máy cá nhân không đủ tài nguyên cho số sandbox cần. Lúc đó chạy cùng file Compose trên server Linux; chỉ cân nhắc Kubernetes khi cần nhiều máy.

## GĐ3 · Mở rộng (liên tục)

Mỗi phòng ban mới là cấu hình cộng MCP server; không sửa lõi.

1. **Chọn control plane dài hạn:** tiếp tục tự viết trên LangGraph hay dùng Paperclip; quyết định trước khi thêm phòng thứ hai.
2. **Phòng Pháp lý:** MCP `legal_search` trên Qdrant (API key chỉ đọc), vai trò Legal Researcher mức L0, bộ eval câu hỏi pháp lý.
3. **Phòng Vận hành:** MCP đọc trạng thái hạ tầng (Docker, server) chỉ đọc, vai trò DevOps mức L0.
4. **Scheduler heartbeat:** job định kỳ tạo ticket (ví dụ quét lỗi CI, báo cáo tuần); job chỉ chạy khi máy đang mở, hoặc đặt trên server nếu cần đúng giờ.
5. **Thêm MCP server** khi có phòng ban cần; mỗi server một container, secret riêng, chỉ nghe trên mạng Docker nội bộ.

**Cổng ra mỗi phòng mới:** có bộ eval riêng đạt ngưỡng trước khi bật cho ticket thật.

## Việc làm xuyên suốt

- **Test:** unit test cho registry, state machine, guard, constraints MCP; test tích hợp cho graph với Postgres thật; eval chạy trong CI.
- **Bảo mật:** mọi tool mới qua review quyền trước khi thêm vào YAML; audit log được kiểm tra định kỳ.
- **Chi phí:** xem dashboard chi phí hằng tuần; đổi model rẻ hơn cho việc đơn giản khi số liệu cho phép.
- **Nâng version:** pin version LangGraph, OpenHands SDK, 9router; nâng version phải qua eval.
- **Provider model:** theo dõi lỗi provider trên 9router; tuyến subscription hoặc free tier đổi thì chuyển sang provider dự phòng trong chuỗi fallback.

## Thứ tự phụ thuộc chính

```text
Bước 0 (dựng máy WSL2 + Docker, quyết định, PoC sandbox Docker)
  └─ GĐ0: repo/CI → core → registry → schema DB → ticket service → lease
          → MCP gitlab → guard → graph giả lập → compose + intake polling
          → observability → spike OpenHands
       └─ GĐ1: sandbox launcher → mạng sandbox → OpenHandsWorker → ngân sách task
               → publish MR → Tech Lead → triage luật → duyệt qua REST → eval → pilot
            └─ GĐ2: Reviewer, QA, Chief of Staff → vòng có trần → merge bot
                    → console, kill switch → metrics → CI + backup → kiểm thử bảo mật
                 └─ GĐ3: control plane → Pháp lý → Vận hành → heartbeat → MCP mới
```

## Tuần đầu tiên

- [ ] Dựng máy: WSL2 Ubuntu, Docker Engine rootless, `.wslconfig`, `uv`
- [ ] Chốt các quyết định ở Bước 0 với người có quyền quyết
- [ ] Bắt đầu PoC sandbox OpenHands bằng workspace Docker với đủ cờ bảo mật
- [ ] Chọn repo pilot và gom 20–30 issue đã đóng làm eval
- [ ] Tạo repo `ai-company` với `uv` workspace và CI lint/test (bước 0.1)
- [ ] Viết model `core/` và registry nạp YAML mẫu (bước 0.2–0.3)
