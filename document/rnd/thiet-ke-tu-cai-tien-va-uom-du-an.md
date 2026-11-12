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
| 2.4 | Canary: sau deploy, ghi `company_sha` vào event `job`; `/rnd/metrics?since_company=<sha>` so N ticket kế tiếp với N ticket trước; xấu đi quá ngưỡng thì tạo ticket escalated "đề xuất revert" kèm lệnh | Không tự revert. **Phần báo cáo đã code (sửa sau review độc lập, nhánh `rnd-canary-fix`, 2026-11-12):** event `job` mang `company_sha` = `<mốc deploy, tối đa 24 ký tự>-<dấu vân tay company/>` (thiếu mốc thì chỉ có dấu vân tay). **Dấu vân tay** băm `org.yaml`, `roles/`, `prompts/`, `sops/`, `policies/`, chỉ file `*.yaml`/`*.yml`/`*.md`, bỏ file và thư mục ẩn, không tính `rnd/watchlist.yaml`. **Mốc deploy** do `aico-ops deploy` đặt: `<git sha 11 ký tự>` khi bản nguồn sạch, `<git sha>x<băm 8 ký tự>` khi bẩn (băm của `git diff HEAD` + `git status --porcelain` + nội dung file chưa theo dõi, nên hai lần deploy bẩn khác nhau ra hai mốc); mốc ghi vào dòng `AICO_COMPANY_SHA` của `~/ai-company/deploy/.env` để `docker compose up` gõ tay vẫn đọc cùng giá trị (`aico-ops mark` in mốc của bản nguồn và mốc đang ghi). **Báo cáo:** `GET /rnd/canary?sha=&n=10&first_pass_drop=15&review_increase=0.5&non_done_increase=15` (thay cho `since_company`) và `aico-ops canary [sha|-] [n]`. Ticket thuộc một cấu hình khi mọi job của nó mang cùng `company_sha`; ticket vắt qua lần deploy bị loại khỏi cả hai nhóm và đếm ở `mixed`. Nhóm sau là N ticket ĐẦU của đợt theo lúc job đầu chạy (không lọc "đã kết thúc" trước: ticket kẹt hay chờ duyệt giữ chỗ, nêu ở `after.pending`, và `complete` chỉ khi cả N đã kết thúc); nhóm trước là N ticket bắt đầu ngay trước đợt (ticket chưa kết thúc nêu ở `before.pending`). Bỏ ticket eval và nhãn research/docs/retro. `regressed` = `true` khi tỷ lệ đạt vòng đầu giảm ≥ 15 điểm, vòng review TB tăng ≥ 0,5, tỷ lệ kết thúc không-done ((escalated + failed) / số đã kết thúc) tăng ≥ 15 điểm, hoặc nhóm sau không ticket nào done mà nhóm trước có; `null` khi chưa đủ mẫu hoặc có tiêu chí thiếu dữ liệu để so mà các tiêu chí khác không báo xấu (thiếu dữ liệu không bao giờ thành `false`); `false` chỉ khi mọi tiêu chí so được và không vượt ngưỡng. **Khớp `sha`:** cả chuỗi; hoặc đoạn đầu (≥ 7 ký tự) của dấu vân tay; hoặc git sha so với mốc sạch (bên ngắn là đoạn đầu bên dài, dán đủ 40 ký tự vẫn khớp); bản bẩn chỉ khớp khi gõ `<sha>x<đoạn đầu băm>` (sha sạch không khớp bản bẩn). **Chưa làm (chờ owner duyệt):** tự tạo ticket escalated "đề xuất revert" |
| 2.5 | Hàng đợi giả thuyết `rnd_topics` (migration): nguồn, mục tiêu, chỉ số, trạng thái `open/testing/confirmed/refuted` | Retro và ticket dò đẩy vào đây; owner chọn cái đem thử |

**Câu hỏi cho owner:**
1. Đồng ý cho `experimenter` ghi vào `VX-Anh/ai-company` (chỉ nhánh `rnd/*`, chỉ prompt + `workers`/`limits`, CI chặn phần còn lại, owner vẫn merge và deploy)? Cần thêm repo này vào token GitHub (Contents + PR, không cần merge).
2. Ngưỡng canary: mặc định "tỷ lệ đạt vòng đầu giảm ≥ 15 điểm, vòng review tăng ≥ 0,5, hoặc tỷ lệ kết thúc không-done (escalated + failed) tăng ≥ 15 điểm trên 10 ticket".

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

Quan sát kèm theo (nhánh `rnd-canary`, 2026-11-12, chưa merge): job review ghi `diff_files`,
`diff_lines` (dòng thêm + xoá của MR so với nhánh đích lúc chấm, gồm cả tài liệu kế hoạch hệ thống
ghi vào branch); `/rnd/metrics` có mục `diff_size`: tỷ lệ đạt vòng đầu theo cỡ diff ở lần review
đầu (bậc 0-50 / 51-200 / 201-1000 / >1000 dòng, và dưới/trên trung vị), để thử giả thuyết P4
"đầu việc diff nhỏ đạt vòng đầu nhiều hơn". Đây là tương quan, không phải nhân quả: việc khó
thường vừa diff lớn vừa nhiều vòng. Ticket hỏng trước lần review đầu không có số đo.
