# Thiết kế R2 (vòng tự cải tiến) và R3 (ươm dự án)

Trạng thái: **bản đề xuất, chờ owner duyệt** (2026-11-12). Phần đã code trên nhánh `rnd-r0`:
rào chắn `aico check-selfimprove` + job CI cho nhánh `rnd/*`, `mcp-research`, watchlist +
`aico-ops scan`, bộ lập lịch dò (mặc định tắt). Phần dưới cần owner quyết vì nó **cho agent ghi
vào chính repo ai-company**.

## R2 · Vòng tự cải tiến

```text
nguồn giả thuyết: /rnd/metrics (lớp lỗi lặp) · retro (đề xuất dạng giả thuyết) · ticket dò công nghệ
        │
        ▼
ticket nhãn selfimprove (project VX-Anh/ai-company, rủi ro high → owner duyệt kế hoạch)
        │  kế hoạch = bản ghi thí nghiệm: giả thuyết, tiêu chí bác bỏ, biến thể, cỡ mẫu
        ▼
experimenter (Codex trước) sửa company/prompts|roles trên nhánh rnd/<ticket>
        │  CI: aico check-selfimprove (chặn mọi thứ ngoài prompt + workers/limits)
        ▼
evaluator (khác provider) chạy bộ chuẩn: baseline vs biến thể (R1, `aico eval run`)
        │  ghi document/rnd/experiments/NNNN-*.md: số đo, kết luận confirmed/refuted/inconclusive
        ▼
PR vào main của ai-company ── owner duyệt ── owner deploy tay ── canary
```

Việc phải làm:

| # | Việc | Ghi chú |
| --- | --- | --- |
| 2.0 | Đặt `selfimprove-scope` (workflow `.github/workflows/selfimprove.yml`, `pull_request_target`, chạy code của `main`) làm **required check** cho `main` của ai-company; PR do agent mở phải mang nhãn `selfimprove` hoặc nhánh `rnd/*` | Đã có trên `rnd-r0`; owner bật required check trong GitHub |
| 2.1 | Vai trò `experimenter` (workers `[codex, claude]`, L2, chỉ repo `VX-Anh/ai-company`, branch `rnd/*`) và `evaluator` (L0 + chạy eval) | mcp-gitlab cần cho phép `rnd/*` cho riêng repo này (`GITLAB_BRANCH_PATTERN` hiện là một mẫu chung `ai/*`: cần đổi thành ánh xạ repo → mẫu) |
| 2.2 | `SelfImprovePlanner` (mẫu `RetroPlanner`): đầu vào `<metrics>` + giả thuyết; đầu ra kế hoạch theo mẫu bản ghi thí nghiệm | Luật rủi ro `selfimprove = high`, `projects: [VX-Anh/ai-company]` |
| 2.3 | `label_scopes`: `selfimprove → (company/prompts/, company/roles/)` (dùng lại cơ chế đã có cho research) | Lớp chặn thứ hai sau CI |
| 2.4 | Canary: sau deploy, ghi `company_sha` vào event `job`; `/rnd/metrics?since_company=<sha>` so N ticket kế tiếp với N ticket trước; xấu đi quá ngưỡng thì tạo ticket escalated "đề xuất revert" kèm lệnh | Không tự revert |
| 2.5 | Hàng đợi giả thuyết `rnd_topics` (migration): nguồn, mục tiêu, chỉ số, trạng thái `open/testing/confirmed/refuted` | Retro và ticket dò đẩy vào đây; owner chọn cái đem thử |

**Câu hỏi cho owner:**
1. Đồng ý cho `experimenter` ghi vào `VX-Anh/ai-company` (chỉ nhánh `rnd/*`, chỉ prompt + `workers`/`limits`, CI chặn phần còn lại, owner vẫn merge và deploy)? Cần thêm repo này vào token GitHub (Contents + PR, không cần merge).
2. Ngưỡng canary: mặc định "tỷ lệ đạt vòng đầu giảm ≥ 15 điểm hoặc vòng review tăng ≥ 0,5 trên 10 ticket".

## R3 · Ươm dự án mới

```text
ý tưởng (owner, ticket dò "Đáng thử", retro) → ticket nhãn spike → kế hoạch có tiêu chí đo
được và trần thời gian (owner duyệt) → experimenter dựng spike trong VX-Anh/aico-lab/spikes/<slug>/
→ evaluator chạy + chụp trên máy build (aico-remote, aico-shot) → bản ghi quyết định
decisions/NNNN-<slug>.md: đi tiếp / dừng / đổi hướng, số đo, rủi ro, ước lượng công, giấy phép
→ owner duyệt PR → nếu "đi tiếp": owner tạo repo thật + ticket sản phẩm mang origin_spike
```

| # | Việc | Ghi chú |
| --- | --- | --- |
| 3.1 | Owner tạo repo `VX-Anh/aico-lab` (private), cấp token như repo pilot | Một repo cho mọi spike, tránh cấp quyền tạo repo cho agent |
| 3.2 | `SpikePlanner`: kế hoạch bắt buộc có "Câu hỏi", "Tiêu chí thành công đo được", "Trần" (số ticket/giờ), "Điều kiện dừng" | Thiếu mục nào thì planner trả lại |
| 3.3 | `label_scopes`: `spike → (spikes/<slug>/, decisions/)`; bỏ Tester (code spike được phép bẩn), Reviewer chấm theo tiêu chí spike | Dùng `test_skip_labels`, `label_briefs` đã có |
| 3.4 | Mẫu ADR và `check_adr.py` trong aico-lab (CI): đủ mục, có số đo, có giấy phép thư viện | Như `check_cards.py` |
| 3.5 | Ticket sản phẩm mang `origin_spike` (nhãn hoặc trường); `/rnd/metrics` đo ước lượng công so với thực tế | Đo chất lượng quyết định của R&D |

**Câu hỏi cho owner:** tạo `VX-Anh/aico-lab` không? Mặc định đề xuất: có, private.

## Chỉ số của phòng (nhắc lại)

Tỷ lệ đạt vòng đầu, vòng review, tỷ lệ owner trả lại, token/ticket (theo tháng) · số thí nghiệm,
tỷ lệ confirmed/refuted · thay đổi giữ được sau canary · spike có quyết định trong trần · hạn mức
R&D tiêu mỗi tuần.
