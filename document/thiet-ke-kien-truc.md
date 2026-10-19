# AI Company OS — Thiết kế kiến trúc & source code

Oct 19, 2026 · @Vu Xuan Anh

## Mục tiêu và nguyên tắc

Source code này dựng một "hệ điều hành công ty": phòng ban, vai trò agent, quy trình và quyền hạn được khai báo bằng cấu hình, rồi chạy thành các agent làm việc thật. Nền tảng gồm LangGraph điều phối, MCP làm lớp tool, OpenHands SDK làm worker viết code và 9router làm cổng model, đứng sau một model relay mỏng trong `aico`.

**Phạm vi v1:** một phòng Kỹ thuật chạy thật trên một repo nội bộ, xử lý ticket từ lúc nhận đến khi có merge request. Kiến trúc dựng sẵn để thêm phòng ban mà không sửa phần lõi.

**Nguyên tắc thiết kế**

1. **Công ty là cấu hình.** Sơ đồ tổ chức, vai trò, SOP và quyền hạn nằm trong YAML; thêm một agent là thêm một file.
2. **Ticket là đơn vị công việc.** Mọi việc đều có ticket, người sở hữu, trạng thái, ngân sách và trace riêng.
3. **Agent không chạm hệ thống trực tiếp.** Mọi tool tác động ra ngoài sandbox đều đi qua guard và MCP server, có kiểm tra quyền theo vai trò. Tool terminal và file của OpenHands chỉ chạy trong sandbox cô lập, không có credential.
4. **Người duyệt theo mức rủi ro.** Đọc thì tự động; merge, deploy, sửa dữ liệu thì chờ người; kế hoạch của ticket rủi ro từ `medium` trở lên cũng chờ người.
5. **Không khóa vào model.** Mọi lời gọi LLM đi qua model relay của `aico` rồi tới 9router; đổi model hay provider là đổi cấu hình 9router.
6. **Bền và quan sát được.** Trạng thái checkpoint vào Postgres, ticket có lease nên chạy tiếp được sau khi `aico` hoặc máy khởi động lại; mỗi bước có trace Langfuse gắn với ticket.
7. **Chạy được trên một máy.** Toàn bộ hệ thống chạy bằng Docker Compose trên một máy Windows (WSL2); cùng file Compose chạy được trên server Linux khi cần chuyển một phần lên đó.

**Ngoài phạm vi v1:** agent tự tuyển agent mới, agent thao tác môi trường production, giao diện quản trị đầy đủ.

## Kiến trúc tổng thể

Hệ thống có sáu tầng; ranh giới quan trọng nhất là tool layer, vì agent chỉ chạm hệ thống thật qua guard và MCP server.

> **Sơ đồ:** [Kiến trúc tổng thể](so-do/kien-truc-tong-the.html) — 6 tầng, mọi tool call đều qua guard.

Đọc từ trên xuống: ticket vào qua kênh, control plane ghi nhận và áp quyền, LangGraph chạy SOP, worker thực thi. Governance đưa quyết định duyệt về graph đang chờ. Mọi lời gọi LLM, kể cả từ sandbox OpenHands, đi qua model relay của `aico` rồi tới 9router. Langfuse nhận hai loại trace gắn ticket id: trace model từ relay và trace workflow từ graph.

## Mô hình tổ chức

Công ty có ba cấp: Board là con người, Chief of Staff điều phối, các phòng ban do trưởng phòng dẫn. Mỗi vai trò là một file YAML, nên thêm phòng ban là thêm cấu hình, không sửa code lõi.

> **Sơ đồ:** [Sơ đồ tổ chức](so-do/so-do-to-chuc.html) — 3 phòng ban, 7 vai trò.

| Vai trò | Loại worker | Tool MCP được phép | Mức tối đa | Giai đoạn |
| --- | --- | --- | --- | --- |
| Tech Lead | `LlmAgent` | gitlab (đọc), ticket | L2 | 1 |
| Coder | `OpenHandsWorker` | gitlab (push branch `ai/*`, mở MR; do `aico` gọi sau khi sandbox xong) | L2 | 1 |
| Reviewer | `LlmAgent` | gitlab (đọc, comment MR) | L2 | 2 |
| QA | `OpenHandsWorker` | gitlab (đọc), chạy test trong sandbox | L1 | 2 |
| Chief of Staff | `LlmAgent` | ticket | L2 | 2 |
| Legal Researcher | `LlmAgent` | legal\_search | L0 | 3 |
| DevOps | `LlmAgent` | hạ tầng chỉ đọc (Docker, server) | L0 | 3 |

Một file vai trò mẫu (số ngân sách và giới hạn là giá trị ví dụ):

```yaml
# company/roles/coder.yaml
id: coder
department: engineering
reports_to: tech_lead
worker: openhands            # openhands | llm
model: coder-model           # tên model hoặc combo khai báo trên 9router
prompt: prompts/coder.md
tools:
  - server: gitlab
    allow: [read_file, create_branch, push, open_merge_request]
    constraints:             # MCP server kiểm tra lại tham số, không chỉ guard
      repos: [group/pilot-repo]
      branch_pattern: "ai/*"
max_risk: L2                 # hành động L3 luôn phải qua người duyệt
budget:
  per_ticket_usd: 5
  monthly_usd: 300
limits:
  max_iterations: 40
  timeout_minutes: 45
```

## Cấu trúc source code

Một monorepo Python quản lý bằng `uv` workspace, tách ba phần: **cấu hình công ty** (`company/`), **lõi chạy** (`src/aico/`) và **MCP server nội bộ** (`mcp_servers/`). Tên package `aico` là tạm, đổi tùy anh.

```text
ai-company/
├── pyproject.toml              # uv workspace, Python 3.12
├── company/                    # "công ty" khai báo bằng YAML
│   ├── org.yaml                # phòng ban, quan hệ báo cáo, người duyệt
│   ├── roles/                  # mỗi vai trò một file: prompt, model, tools, quyền
│   │   ├── chief_of_staff.yaml
│   │   ├── tech_lead.yaml
│   │   ├── coder.yaml
│   │   └── reviewer.yaml
│   ├── sops/                   # quy trình chuẩn, biên dịch thành LangGraph
│   │   └── fix_ticket.yaml
│   └── policies/               # quyền tool, ngưỡng duyệt, ngân sách
│       └── default.yaml
├── src/aico/
│   ├── core/                   # model dữ liệu chung: Ticket, Task, Artifact, Event
│   ├── registry/               # nạp + validate YAML thành Role, Department, SOP
│   ├── tickets/                # ticket store + state machine
│   ├── orchestrator/           # LangGraph
│   │   ├── builder.py          # SOP -> StateGraph
│   │   ├── state.py            # TypedDict state dùng chung
│   │   ├── nodes/              # node dùng chung: triage, plan, delegate, review, approve
│   │   └── checkpoint.py       # PostgresSaver + store dài hạn
│   ├── agents/
│   │   ├── base.py             # Protocol Worker: run(task) -> Result
│   │   ├── llm_agent.py        # agent suy luận (langchain create_agent)
│   │   └── openhands_worker.py # worker viết code qua OpenHands SDK
│   ├── tools/
│   │   ├── mcp_client.py       # MultiServerMCPClient, nạp tool theo vai trò
│   │   └── guard.py            # kiểm tra policy trước mỗi tool call
│   ├── llm_relay/              # endpoint OpenAI-compatible: token theo task, budget, trace, chuyển tới 9router
│   ├── governance/             # approval, budget, audit log, kill switch
│   ├── memory/                 # bộ nhớ dài hạn + truy vấn Qdrant
│   ├── scheduler/              # heartbeat, job định kỳ
│   ├── observability/          # Langfuse callback, metrics Prometheus
│   ├── intake/                 # polling GitLab/Jira lấy ticket mới (webhook khi có server)
│   └── api/                    # FastAPI: REST, console, SSE
├── mcp_servers/                # MCP server tự viết (FastMCP)
│   ├── gitlab/
│   ├── legal_search/           # Qdrant + văn bản pháp luật (giai đoạn 3)
│   └── db_readonly/
├── sandbox_launcher/           # dịch vụ duy nhất giữ quyền Docker: tạo, theo dõi, xóa sandbox
├── console/                    # UI tối giản: hàng chờ duyệt, ticket, chi phí
├── evals/                      # bộ ticket mẫu + script chấm điểm
├── deploy/
│   ├── docker/                 # Dockerfile cho aico, MCP server, launcher, image sandbox
│   ├── compose.yaml            # stack chạy trên máy cá nhân
│   ├── compose.server.yaml     # phần đặt trên server (Langfuse, Qdrant, runner)
│   └── proxy/allowlist.txt     # tên miền sandbox được phép ra
└── tests/
```

Quy tắc phụ thuộc: `core` không import gì khác; `orchestrator` chỉ biết `agents`, `tools`, `governance` qua interface. Nhờ vậy có thể thay OpenHands hoặc LangGraph mà không đụng phần còn lại.

## Thành phần lõi

Có chín thành phần, mỗi cái một package trong `src/aico/`. Ticket và policy là hai thứ phải làm đúng ngay từ đầu; các phần còn lại có thể đơn giản trước, nâng cấp sau.

### Ticket service

- **Trường chính:** `id`, `title`, `body`, `source` (gitlab/jira/api), `department`, `owner_role`, `status`, `risk` (rủi ro ticket `low`/`medium`/`high`, xem mục Bảo mật), `budget_usd`, `spent_usd`, `parent_id`, `artifacts` (link MR, báo cáo), `trace_id`, `lease_owner`, `lease_until`.
- **Trạng thái:** `new`, `triaged`, `planned`, `awaiting_plan_approval`, `in_progress`, `in_review`, `awaiting_merge_approval`, `merging`, `done`; hai trạng thái dừng ngoài luồng chính là `escalated` và `failed`.
- Lưu Postgres; mỗi lần chuyển trạng thái ghi một event để làm audit log.

Mỗi node của graph ứng với một trạng thái ticket; chuyển tiếp có điều kiện như bảng dưới. Bộ đếm vòng nằm trong state của graph.

| Node graph | Trạng thái | Chuyển tiếp |
| --- | --- | --- |
| `triage` | `triaged` | → `plan` |
| `plan` | `planned` | rủi ro `low` → `code`; `medium`/`high` → `approve_plan` |
| `approve_plan` | `awaiting_plan_approval` | duyệt → `code`; cần sửa → `plan` (`plan_rounds` + 1); từ chối, quá vòng hoặc quá hạn → `escalate` |
| `code` | `in_progress` | sandbox sửa code, `aico` push branch và mở MR → `review` |
| `review` | `in_review` | đạt → `approve_merge`; chưa đạt → `code` (`review_rounds` + 1); quá vòng → `escalate` |
| `approve_merge` | `awaiting_merge_approval` | duyệt → `merge`; yêu cầu sửa → `code` (`review_rounds` + 1); từ chối, quá vòng hoặc quá hạn → `escalate` |
| `merge` | `merging` | GitLab xác nhận MR đã merged → `finish`; conflict hoặc pipeline lỗi → `code` (`merge_attempts` + 1); quá lần → `escalate` |
| `finish` | `done` | đóng ticket, ghi bài học vào memory |
| `escalate` | `escalated` | người nhận ticket xử lý tay hoặc resume graph |
| bất kỳ | `failed` | lỗi kỹ thuật không phục hồi sau số lần retry (sandbox, MCP server), hoặc người duyệt đóng ticket |

### Registry

Nạp toàn bộ `company/` lúc khởi động, validate bằng Pydantic. Cấu hình sai thì từ chối khởi động. Môi trường dev có hot-reload để chỉnh prompt nhanh.

### Orchestrator (LangGraph)

- Mỗi SOP biên dịch thành một `StateGraph`, ghép từ các node dùng chung: `triage`, `plan`, `delegate`, `review`, `approve`.
- Mô hình supervisor phân cấp: Chief of Staff định tuyến ticket về phòng ban, trưởng phòng lập kế hoạch và giao việc cho worker.
- `thread_id` = ticket id, checkpoint bằng `AsyncPostgresSaver` sau mỗi bước. Checkpoint chỉ giữ state; nó không tự chạy lại công việc khi tiến trình chết.
- **Nhận lại việc:** tiến trình `aico` nhận ticket bằng `SELECT ... FOR UPDATE SKIP LOCKED` rồi ghi `lease_owner`, `lease_until` và gia hạn lease định kỳ.
    - `aico` crash, máy ngủ hay tắt thì lease hết hạn. Khi `aico` chạy lại, scheduler nhận lại ticket và gọi lại graph cùng `thread_id` để chạy tiếp từ checkpoint cuối.
    - Trên một máy chỉ có một tiến trình `aico`. Cơ chế lease vẫn giữ, để sau này chạy thêm tiến trình trên server mà mỗi ticket vẫn chỉ có một nơi xử lý.
- **Side effect idempotent:** một node có thể chạy lại từ đầu, sau restart và cả khi resume sau `interrupt()`. Mọi side effect mang idempotency key `ticket_id:node:attempt`; trước khi tạo branch hoặc MR, node đối soát với GitLab xem đã có chưa.
- Node `approve_plan` và `approve_merge` gọi `interrupt()` để dừng và chờ. Node không tự chuyển ticket sang `awaiting_*`: runner chuyển sau khi checkpoint có interrupt đã lưu, để tiến trình chết giữa chừng không để ticket kẹt ở trạng thái chờ; console hoặc REST API gửi `Command(resume={...})` mang quyết định duyệt. `interrupt()` không tự áp chính sách: node đọc giá trị resume, ghi bản ghi duyệt vào Governance rồi rẽ nhánh theo kết quả.
- **Giới hạn vòng lặp:** state có `plan_rounds`, `review_rounds`, `merge_attempts`; node tăng bộ đếm khi bị quay lại. Mỗi bộ đếm có trần khai báo trong SOP, mỗi điểm chờ duyệt có hạn `approval_deadline`; quá trần hoặc quá hạn thì chuyển `escalated`.

### Agent runtime

Hai loại worker dùng chung một interface, nên orchestrator không cần biết bên dưới là gì:

```python
class Worker(Protocol):
    role: RoleSpec
    async def run(self, task: Task, ctx: RunContext) -> Result: ...
```

- **`LlmAgent`:** lập kế hoạch, phân tích, review. Dựng bằng `create_agent` với đúng các tool MCP vai trò được phép.
- **`OpenHandsWorker`:** lớp trong `aico` xin `sandbox_launcher` một container sandbox riêng cho mỗi task (OpenHands SDK, workspace Docker). Trong sandbox, agent OpenHands chỉ dùng tool terminal và file editor của nó trên workspace đó (hành động L1) và gọi model qua model relay của `aico` bằng token tạm của task; sandbox không được nạp tool MCP.
- **Sandbox không giữ credential GitLab:** `aico` lấy mã nguồn qua MCP `gitlab`, launcher chép vào một Docker volume làm workspace trước khi chạy sandbox. Sandbox trả diff và log về `aico`. Sau đó `aico` mới push branch `ai/*` và mở MR qua guard, với quyền của vai trò Coder.
- **`sandbox_launcher`:** dịch vụ nhỏ, là nơi duy nhất giữ quyền Docker. Nó chỉ có API tạo sandbox theo một mẫu cố định, lấy kết quả và xóa sandbox; `aico` và sandbox không bao giờ thấy Docker socket.

### Tool layer (MCP)

- MCP client chỉ nạp tool có trong mục `tools:` của vai trò; không khai báo thì agent không thấy tool.
- `guard.py` bọc mỗi tool call: kiểm tra quyền đọc/ghi, mức rủi ro và ngân sách. Hành động cần duyệt được chuyển thành yêu cầu duyệt thay vì thực thi.
- MCP server tự kiểm tra lại tham số theo `constraints` của vai trò (repo, branch), không chỉ dựa vào guard.
- Tool `merge_mr` chỉ chạy khi kèm id một bản ghi duyệt hợp lệ do Governance cấp.
- Mỗi tool call ghi audit: vai trò nào, ticket nào, tham số (đã che secret), kết quả.

### Model relay

9router là cổng tới nhiều provider nhưng không có key riêng theo vai trò hay task, không có hạn mức theo key và không gửi trace sang Langfuse. Model relay bù các phần đó:

- Endpoint tương thích OpenAI (`/v1/chat/completions`, có streaming) chạy trong `aico`. Mọi lời gọi LLM của `LlmAgent` và sandbox đi qua đây rồi mới tới 9router.
- Mỗi task nhận một token tạm gắn với `ticket_id` và vai trò, hết hạn khi task xong. Chỉ relay giữ API key của 9router; sandbox không thấy key này.
- Relay kiểm tra vai trò được dùng model nào (theo YAML), thực thi ngân sách, và ghi trace model sang Langfuse.
- Relay chỉ chuyển tiếp, không sửa nội dung request hay response.

### Governance

- **Ngân sách** do model relay thực thi, vì 9router chỉ có hạn mức theo provider.
    - Ngân sách tính bằng token và quy ra USD theo bảng giá khai báo trong `policies/`. Tuyến subscription có chi phí biên gần bằng 0 nhưng quota có hạn, nên token vẫn là đơn vị chính.
    - Mỗi vai trò có hạn mức tháng; mỗi ticket có trần riêng.
    - Trước mỗi lời gọi, relay giữ chỗ một khoản ước tính trong Postgres, khóa theo ticket để request đồng thời và retry không vượt trần. Sau lời gọi, relay đối soát bằng số token thật trong `usage` của response.
    - Hết ngân sách thì relay từ chối request, ticket dừng và hỏi người.
    - Hạn mức theo provider trên 9router là lớp chặn cuối cho toàn hệ thống.
- **Hàng chờ duyệt:** danh sách các `interrupt` đang chờ, hiện trên console; quá hạn thì escalate. Mỗi quyết định duyệt ghi thành bản ghi (ai, lúc nào, cho hành động nào) rồi gửi về graph bằng `Command(resume=...)`.
- **Kill switch:** tạm dừng một agent, một phòng ban hoặc toàn bộ công ty.

### Memory

- **Ngắn hạn:** state của graph, sống theo ticket.
- **Dài hạn:** LangGraph store trên Postgres, lưu quyết định và bài học sau mỗi ticket (ví dụ: repo nào chạy test bằng lệnh gì).
- **Tri thức:** Qdrant cho tài liệu nội bộ và văn bản pháp luật, truy cập qua MCP server `legal_search`.

### Observability

- Langfuse, `session_id` = ticket id, nhận hai loại trace nối với nhau qua `ticket_id`:
    - Trace workflow do graph gửi: mỗi node một span, log OpenHands đính kèm.
    - Trace model do model relay gửi: mỗi lời gọi LLM một bản ghi, kèm model thật 9router đã dùng sau fallback.
- Metrics: ticket theo trạng thái, tỉ lệ MR được merge, chi phí mỗi ticket, thời gian chờ duyệt.

## Luồng xử lý mẫu

Luồng chuẩn của phòng Kỹ thuật có hai điểm người duyệt và một vòng sửa có giới hạn. Giai đoạn 1 chạy bản rút gọn: phân loại bằng luật, review do người làm.

> **Sơ đồ:** [Luồng xử lý ticket](so-do/luong-xu-ly-ticket.html) — 2 điểm duyệt, 1 vòng sửa.

Mỗi vòng có trần riêng: sửa kế hoạch, review và merge lỗi. Quá trần hoặc quá hạn duyệt thì ticket chuyển `escalated`. Mọi điểm dừng đều là checkpoint, nên người duyệt sau vài giờ vẫn tiếp tục được. Ticket chỉ chuyển `done` khi GitLab xác nhận MR đã merged.

Khung LangGraph tương ứng (phác thảo, đối chiếu docs LangGraph khi viết thật):

```python
from langgraph.graph import StateGraph, START, END

MAX_PLAN_ROUNDS = 2
MAX_REVIEW_ROUNDS = 3
MAX_MERGE_ATTEMPTS = 2

def route_plan(s: TicketState) -> str:
    return "code" if s["risk"] == "low" else "approve_plan"

def route_plan_approval(s: TicketState) -> str:
    if s["plan_decision"] == "approved":
        return "code"
    if s["plan_decision"] == "revise" and s["plan_rounds"] < MAX_PLAN_ROUNDS:
        return "plan"
    return "escalate"   # từ chối, quá vòng hoặc quá hạn duyệt

def route_review(s: TicketState) -> str:
    if s["review_passed"]:
        return "approve_merge"
    return "code" if s["review_rounds"] < MAX_REVIEW_ROUNDS else "escalate"

def route_merge_approval(s: TicketState) -> str:
    if s["merge_decision"] == "approved":
        return "merge"
    if s["merge_decision"] == "changes_requested" and s["review_rounds"] < MAX_REVIEW_ROUNDS:
        return "code"
    return "escalate"

def route_merge(s: TicketState) -> str:
    if s["merged"]:     # GitLab xác nhận MR đã merged
        return "finish"
    return "code" if s["merge_attempts"] < MAX_MERGE_ATTEMPTS else "escalate"

g = StateGraph(TicketState)
g.add_node("triage", triage)                # Chief of Staff: phòng ban, rủi ro ticket
g.add_node("plan", plan)                    # Tech Lead; tăng plan_rounds khi bị trả lại
g.add_node("approve_plan", approve_plan)    # interrupt()
g.add_node("code", run_openhands)           # sandbox sửa code; aico push + mở MR qua guard
g.add_node("review", review)                # Reviewer + CI; tăng review_rounds khi chưa đạt
g.add_node("approve_merge", approve_merge)  # interrupt(), hành động L3
g.add_node("merge", merge)                  # merge_mr kèm id bản ghi duyệt; tăng merge_attempts khi lỗi
g.add_node("escalate", escalate)
g.add_node("finish", finish)

g.add_edge(START, "triage")
g.add_edge("triage", "plan")
g.add_conditional_edges("plan", route_plan)
g.add_conditional_edges("approve_plan", route_plan_approval)
g.add_edge("code", "review")
g.add_conditional_edges("review", route_review)
g.add_conditional_edges("approve_merge", route_merge_approval)
g.add_conditional_edges("merge", route_merge)
g.add_edge("escalate", END)
g.add_edge("finish", END)
```

Node chờ duyệt chạy lại từ dòng đầu khi resume, nên không đặt side effect trước `interrupt()`:

```python
from langgraph.types import interrupt

async def approve_plan(s: TicketState) -> dict:
    decision = interrupt({"kind": "plan", "ticket_id": s["ticket_id"], "plan": s["plan"]})
    # idempotent theo ticket + vòng: chạy lại không tạo bản ghi trùng
    await governance.record_approval(s["ticket_id"], "plan", s["plan_rounds"], decision)
    return {"plan_decision": decision["decision"]}
```

Chạy và resume cùng `thread_id`:

```python
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command

# Mở saver một lần trong lifespan của FastAPI; saver.setup() chạy một lần lúc migrate.
async with AsyncPostgresSaver.from_conn_string(DB_URI) as saver:
    graph = g.compile(checkpointer=saver)

    async def run_ticket(ticket_id: str, inputs: dict | None = None, resume: dict | None = None):
        config = {"configurable": {"thread_id": ticket_id}}
        payload = Command(resume=resume) if resume is not None else inputs
        return await graph.ainvoke(payload, config)

    # lần đầu:   await run_ticket("T-123", inputs={"ticket_id": "T-123", ...})
    # sau duyệt: await run_ticket("T-123", resume={"decision": "approved", "approval_id": "A-9"})
```

Lỗi kỹ thuật không phục hồi được sau số lần retry do node ném ra; lớp chạy graph bắt lỗi, chuyển ticket sang `failed` và nhả lease.

## Hạ tầng và triển khai

Hệ thống chạy chủ yếu trên một máy cá nhân Windows 10/11: Docker Engine cài trong WSL2 (Ubuntu), toàn bộ dịch vụ khai báo trong một file Docker Compose. Thành phần nặng hoặc dùng chung có thể đặt trên server Linux, chạy bằng file Compose riêng. Không dùng Kubernetes; chỉ cân nhắc khi cần nhiều máy hoặc nhiều tiến trình `aico`.

| Thành phần | Chạy ở đâu | Ghi chú |
| --- | --- | --- |
| `aico` (API + orchestrator) | Máy cá nhân, container hoặc chạy thẳng trong WSL2 khi dev | Một tiến trình; state nằm ở Postgres, có lease nên chạy tiếp được sau khởi động lại |
| `sandbox_launcher` | Máy cá nhân, container | Duy nhất giữ quyền Docker (ưu tiên Docker rootless) |
| Sandbox OpenHands | Máy cá nhân, mỗi task một container, xóa khi xong | Không có credential GitLab; tối đa N sandbox cùng lúc (ví dụ 2) để giữ tài nguyên máy |
| MCP server | Máy cá nhân, mỗi server một container | Chỉ nghe trên mạng Docker nội bộ; mỗi server một file secret riêng |
| Egress proxy | Máy cá nhân, container | Cửa ra duy nhất của sandbox, lọc theo tên miền |
| Postgres | Máy cá nhân, container với volume | Checkpoint LangGraph, ticket, audit, store dài hạn; `pg_dump` định kỳ ra nơi khác |
| Model relay | Trong `aico` | Token tạm theo task, ngân sách, trace model; nơi duy nhất gọi 9router |
| 9router | Máy cá nhân, container | Một cổng tới nhiều provider, fallback giữa subscription, model trả phí và free tier; hạn mức theo provider; chỉ nhận kết nối từ `aico` |
| Langfuse | Server hoặc Langfuse Cloud | Bản tự host cần thêm ClickHouse, Redis và object storage, nặng cho máy cá nhân |
| Qdrant | Server hoặc máy cá nhân | Chỉ cần từ GĐ3; collection riêng, API key chỉ đọc |
| Secrets | File secret ngoài repo, mount qua Docker secrets | Chỉ container MCP đọc được; dùng Vault nếu server có sẵn |
| CI/CD | GitLab CI, runner trên server | lint → test → eval smoke → build image; deploy bằng `docker compose pull && up -d` |

**Máy cá nhân:**
- Cài WSL2 với Ubuntu, Docker Engine bên trong WSL2 (miễn phí). Docker Desktop cũng dùng được nhưng cần license với doanh nghiệp từ 250 nhân viên hoặc doanh thu từ 10 triệu USD/năm.
- Giới hạn RAM, CPU cho WSL2 trong `%UserProfile%\.wslconfig` (ví dụ `memory=12GB`) để Windows vẫn còn tài nguyên.
- Repo và workspace sandbox để trong ổ của WSL (`~/...`), không để dưới `/mnt/c`, vì truy cập ổ Windows từ WSL chậm hơn nhiều.
- Máy có thể ngủ hoặc tắt: ticket dừng ở checkpoint và chạy tiếp khi máy mở lại. Webhook từ GitLab không vào được máy cá nhân, nên `aico` lấy ticket bằng polling API GitLab/Jira (ví dụ mỗi phút); webhook chỉ dùng khi có thành phần nhận trên server.

**Server (tùy chọn):** đặt những thứ dùng chung, nặng hoặc cần chạy liên tục: Langfuse, Qdrant, GitLab Runner. Máy cá nhân nối tới server qua mạng nội bộ hoặc VPN, mọi kết nối có TLS và API key. Sau này muốn chạy 24/7 thì đưa cả stack lên server bằng cùng file Compose.

**Mạng (Docker trên máy cá nhân):**
- Sandbox nằm trong một mạng Docker `internal: true`, không có đường ra Internet; trong mạng đó chỉ có endpoint model relay của `aico` và egress proxy.
- Egress proxy chỉ cho đi tới mirror package registry theo `allowlist.txt`; không tới 9router, GitLab, Docker socket hay mạng LAN.
- MCP server và Postgres nằm trong mạng nội bộ riêng của `aico`, không publish cổng ra máy (trừ cổng API/console của `aico` chỉ bind `127.0.0.1`).

## Bảo mật và quản trị

Có hai thang rủi ro tách biệt. **Mức hành động** (L0–L3) do guard gán cho từng tool call; mỗi vai trò khai báo mức tối đa trong YAML, và hành động L3 luôn bắt buộc người duyệt. **Rủi ro ticket** (`low`/`medium`/`high`) do bước phân loại gán cho cả ticket; nó chỉ quyết định kế hoạch có cần người duyệt trước khi code hay không.

| Mức | Loại hành động | Ví dụ | Cơ chế |
| --- | --- | --- | --- |
| L0 | Đọc | Đọc code, tìm văn bản, đọc ticket | Tự động, có audit |
| L1 | Ghi trong sandbox | Sửa file, chạy test, commit lên branch riêng | Tự động trong workspace cô lập |
| L2 | Ghi ra ngoài, đảo ngược được | Mở MR, comment ticket, tạo ticket con | Tự động, có audit và thông báo |
| L3 | Khó đảo ngược hoặc tác động thật | Merge, deploy, sửa dữ liệu, gửi ra ngoài công ty, vượt ngân sách | Dừng bằng `interrupt()`, chờ người duyệt |

Rủi ro ticket và các điểm duyệt (tiêu chí phân loại là giá trị ví dụ, team chốt lại):

| Rủi ro ticket | Ví dụ | Duyệt kế hoạch | Duyệt merge (L3) |
| --- | --- | --- | --- |
| `low` | Sửa trong một module có test, không chạm cấu hình | Không | Có |
| `medium` | Nhiều module, thiếu test, đổi API nội bộ | Có | Có |
| `high` | Chạm xác thực, phân quyền, dữ liệu hoặc hạ tầng | Có, trưởng nhóm duyệt | Có |

**Các lớp bảo vệ khác**

- **Sandbox:** container chạy user non-root, `--cap-drop ALL`, `--security-opt no-new-privileges`, root filesystem `--read-only` (chỉ workspace và `/tmp` ghi được), giới hạn `--memory`, `--cpus`, `--pids-limit` và thời gian; không mount Docker socket hay thư mục của máy; không giữ credential GitLab; xóa sau mỗi task.
- **Quyền Docker:** chỉ `sandbox_launcher` giữ quyền Docker, ưu tiên Docker rootless để container có thoát ra cũng không có quyền root trên máy. Launcher chỉ nhận yêu cầu từ `aico` và chỉ tạo sandbox theo mẫu cố định (image, cờ bảo mật, mạng), không nhận tham số tùy ý.
- **Secret:** agent không bao giờ thấy secret thô. Credential nằm trong MCP server; agent chỉ gọi tool.
- **Git:**
    - GitLab không giới hạn token theo tên branch, nên ràng buộc `ai/*` thực thi tại MCP server `gitlab`: push sang repo hoặc branch không khớp `constraints` bị từ chối.
    - Phía GitLab, bot của Coder chỉ có vai trò Developer trên repo pilot.
    - `main` và `release/*` là protected branch. "Allowed to push" không gồm bot nào; "Allowed to merge" chỉ gồm Maintainer và một tài khoản merge bot riêng.
    - Merge bot chỉ chạy qua tool `merge_mr`, tool này đòi bản ghi duyệt hợp lệ.
- **Prompt injection:** nội dung ticket, file, trang web là dữ liệu, không phải lệnh. Guard chặn hành động L3 dù prompt yêu cầu; vai trò đọc dữ liệu bên ngoài không có quyền ghi.
- **Kill switch và audit:** dừng được từng agent hoặc toàn bộ; mọi tool call và mọi quyết định duyệt đều tra lại được.

## Lộ trình triển khai

Xây theo bốn giai đoạn, mỗi giai đoạn có cổng ra đo được. GĐ0 và GĐ1 phải làm chắc trước khi nghĩ đến nhiều phòng ban.

> **Sơ đồ:** [Lộ trình triển khai](so-do/lo-trinh-trien-khai.html) — 4 giai đoạn, mỗi giai đoạn một cổng ra.

Thời lượng là ước lượng; ngưỡng ở mỗi cổng do team chốt trước khi bắt đầu giai đoạn đó. Các bước chi tiết nằm trong [kế hoạch triển khai](ke-hoach-trien-khai.md).

**Việc đầu tiên của GĐ0**

- [ ] Tạo repo `ai-company`, `uv` workspace, CI chạy lint và test
- [ ] Viết model `Ticket`, `RoleSpec` và registry nạp thư mục `company/`
- [ ] Docker Compose cho dev: Postgres và MCP server `gitlab`
- [ ] Graph `fix_ticket` tối thiểu có `interrupt()`, checkpoint vào Postgres
- [ ] Lease theo ticket và vòng nhận lại việc; thử kill `aico` giữa chừng
- [ ] Spike OpenHands SDK trỏ về model relay → 9router, chạy một task trên repo thử

## Quyết định còn mở và rủi ro

Cần chốt bảy điểm dưới đây trước Giai đoạn 0. Rủi ro lớn nhất là chất lượng worker trên codebase thật, không phải phần điều phối.

**Cần chốt**

- [ ] Công ty AI này phục vụ TVPL hay là dự án riêng? Câu trả lời quyết định nơi deploy và dữ liệu được phép truy cập.
- [ ] Nguồn ticket là GitLab Issues hay Jira?
- [ ] Model hoặc combo cho từng vai trò trên 9router: tuyến subscription, API key hay free tier? Ảnh hưởng trực tiếp chất lượng, quota và chi phí.
- [ ] Sandbox OpenHands trên máy Windows: PoC workspace Docker qua `sandbox_launcher` với Docker rootless trong WSL2; xác nhận chạy được với các cờ bảo mật ở mục Bảo mật.
- [ ] Phần nào đặt trên server: Langfuse, Qdrant đã có sẵn ở đâu chưa, hay dùng Langfuse Cloud? Máy cá nhân có bao nhiêu RAM, có GPU không?
- [ ] Control plane ở Giai đoạn 3: tự viết trên LangGraph hay dùng Paperclip (Node.js)?
- [ ] Repo pilot cho Giai đoạn 1: nên chọn repo có test tự động tốt.

**Rủi ro**

| Rủi ro | Dấu hiệu | Giảm thiểu |
| --- | --- | --- |
| Worker làm sai trên codebase lớn | MR bị từ chối nhiều | Bắt đầu bằng ticket nhỏ, có test; đo trên bộ eval trước khi mở rộng |
| Chi phí token tăng nhanh khi nhiều agent | `spent_usd` vượt kế hoạch | Trần ngân sách theo ticket và vai trò; model rẻ cho việc đơn giản |
| Lỗi cộng dồn qua nhiều tầng agent | Ticket kẹt ở vòng review | Ít tầng; giới hạn vòng lặp; escalate sớm |
| Prompt injection từ ticket hoặc tài liệu | Agent đòi tool ngoài phạm vi | Guard theo policy; L3 luôn qua người |
| Sandbox vượt guard qua terminal | Request lạ từ container sandbox | Sandbox không có credential, chỉ gọi được model relay và registry |
| Tài khoản subscription bị khóa hoặc tuyến OAuth thay đổi | 9router báo lỗi provider, ticket dừng | Giữ ít nhất một provider dùng API key trong chuỗi fallback; đọc điều khoản từng provider; ticket dừng ở checkpoint và chạy tiếp khi có model |
| Sandbox thoát ra máy cá nhân | Tiến trình lạ trên WSL2 | Docker rootless, cờ bảo mật container, chỉ launcher giữ quyền Docker |
| Máy cá nhân hết tài nguyên hoặc tắt | Task chậm, ticket đứng lâu | Giới hạn số sandbox, `.wslconfig`; checkpoint và lease để chạy tiếp; đưa phần nặng lên server |
| Mất dữ liệu trên máy cá nhân | Hỏng ổ, xóa nhầm volume | `pg_dump` định kỳ ra server hoặc ổ khác; repo code đã nằm trên GitLab |
| Side effect lặp khi node chạy lại | Branch hoặc MR trùng | Idempotency key; đối soát GitLab trước khi tạo |
| API framework thay đổi nhanh | Nâng version làm gãy code | Bọc LangGraph và OpenHands sau interface; pin version; chạy eval trong CI |
