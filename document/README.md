# AI Company OS — Tài liệu dự án

Bộ tài liệu thiết kế của AI Company OS: một "hệ điều hành công ty" gồm phòng ban, vai trò agent, quy trình (SOP) và quyền hạn khai báo bằng YAML. Agent chạy trên LangGraph (điều phối), MCP (tool), OpenHands SDK (worker viết code) và 9router (cổng model, đứng sau model relay của `aico`). Chạy chủ yếu trên một máy Windows bằng WSL2 và Docker Compose.

- Cập nhật: 2026-10-19
- Tác giả: Vu Xuan Anh
- Bản gốc: [Claude Docs](https://claude.ai/code/artifact/56a94fc6-a4e1-4380-b0ea-ca4500fb4f48)

## Cấu trúc thư mục

```text
document/
├── README.md                    # file này: mục lục bộ tài liệu
├── thiet-ke-kien-truc.md        # tài liệu thiết kế chính
├── ke-hoach-trien-khai.md       # kế hoạch thực hiện từng bước theo giai đoạn
└── so-do/                       # sơ đồ tương tác (mở bằng trình duyệt)
    ├── kien-truc-tong-the.html
    ├── so-do-to-chuc.html
    ├── luong-xu-ly-ticket.html
    ├── lo-trinh-trien-khai.html
    └── nguon/                   # file JSON nguồn để dựng lại sơ đồ bằng archify
        ├── kien-truc-tong-the.json
        ├── so-do-to-chuc.json
        ├── luong-xu-ly-ticket.json
        └── lo-trinh-trien-khai.json
```

## Tài liệu thiết kế

[thiet-ke-kien-truc.md](thiet-ke-kien-truc.md)

| Mục | Nội dung |
| --- | --- |
| [Mục tiêu và nguyên tắc](thiet-ke-kien-truc.md#mục-tiêu-và-nguyên-tắc) | Phạm vi v1, 6 nguyên tắc thiết kế, ngoài phạm vi |
| [Kiến trúc tổng thể](thiet-ke-kien-truc.md#kiến-trúc-tổng-thể) | 6 tầng: kênh vào, control plane, điều phối, worker, tool layer, nền tảng |
| [Mô hình tổ chức](thiet-ke-kien-truc.md#mô-hình-tổ-chức) | Board, Chief of Staff, 3 phòng ban, 7 vai trò; file YAML mẫu |
| [Cấu trúc source code](thiet-ke-kien-truc.md#cấu-trúc-source-code) | Monorepo `uv`: `company/`, `src/aico/`, `mcp_servers/` |
| [Thành phần lõi](thiet-ke-kien-truc.md#thành-phần-lõi) | Ticket (bảng chuyển trạng thái), Registry, Orchestrator (lease, idempotent, giới hạn vòng), Agent runtime, Tool layer, Governance, Memory, Observability |
| [Luồng xử lý mẫu](thiet-ke-kien-truc.md#luồng-xử-lý-mẫu) | Luồng sửa ticket, khung LangGraph, ví dụ `interrupt()` và chạy/resume |
| [Hạ tầng và triển khai](thiet-ke-kien-truc.md#hạ-tầng-và-triển-khai) | Một máy Windows (WSL2 + Docker Compose), phần đặt trên server, mạng sandbox |
| [Bảo mật và quản trị](thiet-ke-kien-truc.md#bảo-mật-và-quản-trị) | Mức hành động L0–L3, rủi ro ticket, các lớp bảo vệ |
| [Lộ trình triển khai](thiet-ke-kien-truc.md#lộ-trình-triển-khai) | 4 giai đoạn và việc đầu tiên của GĐ0 |
| [Quyết định còn mở và rủi ro](thiet-ke-kien-truc.md#quyết-định-còn-mở-và-rủi-ro) | Việc cần chốt và bảng rủi ro |

## Kế hoạch triển khai

[ke-hoach-trien-khai.md](ke-hoach-trien-khai.md): Bước 0 (chốt quyết định, PoC sandbox), rồi GĐ0 → GĐ3. Mỗi bước ghi rõ việc, đầu ra và điều kiện xong; mỗi giai đoạn có cổng ra.

## Sơ đồ

| Sơ đồ | Cho thấy | Loại archify |
| --- | --- | --- |
| [Kiến trúc tổng thể](so-do/kien-truc-tong-the.html) | Các thành phần và đường gọi; sandbox cô lập, mọi tool call ra ngoài đều qua guard | `architecture` |
| [Sơ đồ tổ chức](so-do/so-do-to-chuc.html) | Board → Chief of Staff → phòng ban → vai trò | `tree` |
| [Luồng xử lý ticket](so-do/luong-xu-ly-ticket.html) | Từ ticket mới đến merge đã xác nhận; 2 điểm người duyệt, các vòng sửa có trần, escalate | `workflow` |
| [Lộ trình triển khai](so-do/lo-trinh-trien-khai.html) | GĐ0 → GĐ3, mỗi giai đoạn mở khi qua cổng của giai đoạn trước | `workflow` |

Dựng lại một sơ đồ sau khi sửa file nguồn (chạy từ thư mục gốc của repo):

```bash
node ~/.claude/skills/archify/bin/archify.mjs finalize <architecture|tree|workflow> \
  document/so-do/nguon/<ten>.json document/so-do/<ten>.html --quality showcase --json
```
