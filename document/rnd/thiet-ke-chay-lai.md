# Thiết kế R1: chạy lại ticket cũ để đo thay đổi của aico

Trạng thái (2026-11-12): **đã code trên nhánh `rnd-r1`** (tách từ `rnd-r0`), theo mặc định ở
mục 6 (branch `eval/*` trên repo thật, không fork). Owner duyệt nhánh này riêng; `rnd-r0` merge
được độc lập. Chưa chạy thật: cần merge, deploy, bật `aico-eval`.

**Cách dùng (sau khi merge + deploy):**

```bash
# 1. Bộ chuẩn: lấy ứng viên, chọn 10–15 mục, lưu suite.json (giữ trường base_sha, observed)
aico-ops bench 90 > suite.json
# 2. Biến thể: thư mục company/ đã sửa (prompt, workers, limits), đặt trong .env
#    AICO_EVAL_COMPANY_DIR=/đường/dẫn/biến-thể   (để trống = company/ hiện tại = baseline)
docker compose --profile eval up -d aico-eval
# 3. Chạy: mỗi mục 2 lần
aico-ops eval-start baseline 2 < suite.json
# 4. Xem, rồi đóng các ticket đã chấm
aico-ops eval-report baseline < suite.json
aico-ops eval-report baseline --close
```

Đã làm: nhãn + artifact `eval`, `workplan.ticket_refs` (nhánh `eval/<id>` từ commit gốc, MR vào
`eval/base/<id>`), `TicketStore.claim(eval_only, eval_variant)`, `AICO_EVAL_INSTANCE`,
`AICO_EVAL_VARIANT`, bỏ duyệt kế hoạch và chặn merge trong code (không theo luật YAML),
mcp-gitlab/guard nhiều mẫu nhánh và ref SHA, `aico eval start|report`, service `aico-eval`
(profile `eval`), chỉ số chung không tính ticket eval. Chưa làm: chấm acceptance (build/test trên
máy build) tự động; dọn branch `eval/*` sau khi chấm; trần hạn mức riêng cho eval (hiện chỉ dựa
vào việc owner bật/tắt `aico-eval`); `base_sha` nên là merge-base (compare API).

## 1. Mục tiêu

Đo một thay đổi của aico (prompt, thứ tự `workers`, `limits`, chèn `<knowledge>`, ngưỡng xếp
phòng) trên cùng một bộ đề bài, thay vì đổi thẳng trên ticket thật rồi đoán. Kết quả là một bản
ghi thí nghiệm có số đo, owner đọc rồi quyết định áp hay không.

Không làm: tự áp thay đổi; chạy trên repo thật có quyền merge; dùng API trả phí để chấm.

## 2. Bộ ticket chuẩn

- Nguồn: `aico-ops bench 90` (ticket code đã merge, có `base_sha`), chọn 10–15 ticket trải đều:
  dễ (đạt vòng đầu) / khó (≥ 2 vòng review hoặc người trả lại), đủ repo (aico-office,
  linh-hoa-vien, ai-trace). Thêm 5 ticket tổng hợp cho lớp lỗi hay gặp nhất ở `/rnd/metrics`.
- Lưu: `eval/tickets/<id>.yaml` trong ai-company:

```yaml
id: T-8d17b253
project: VX-Anh/aico-office
title: ...
body: ...
labels: [...]
base_sha: <commit main lúc ticket bắt đầu>
reference_sha: <merge commit đã duyệt>    # chỉ để so, không đưa cho agent
acceptance:                              # cách chấm tự động
  build: "npm ci && npm run build"
  test: "npm test"
holdout: false                           # true: tập kiểm, experimenter không thấy kết quả từng ticket
observed: {code_rounds: 2, review_rounds: 2, owner_returns: 0, tokens: 182000}
```

- Tập kiểm (`holdout: true`, khoảng 1/3): chỉ báo con số gộp cho experimenter, tránh tối ưu vẹt.

## 3. Cơ chế chạy lại

Dùng nguyên graph `fix_ticket` (plan → code → review → test) để đo đúng hệ thật, với các khác
biệt gói trong nhãn `eval`:

| Điểm | Ticket thường | Ticket `eval` |
| --- | --- | --- |
| Ai nhận ticket | runner của aico chính | **chỉ** runner của instance eval có cùng `variant` (lọc khi `claim`) |
| Cấu hình công ty | `company/` | `AICO_COMPANY_DIR` trỏ tới thư mục biến thể |
| Branch làm việc | `ai/<ticket>` từ `main` | `eval/<ticket>` từ `base_sha` |
| Đích của MR | `main` | `eval/base/<ticket>` (branch tạo ở `base_sha`) — diff đúng phần agent làm |
| Duyệt kế hoạch | theo rủi ro | bỏ (triage ép rủi ro low trong code), vì đo hệ chứ không đo người |
| Merge | owner duyệt | **không bao giờ**; tới `awaiting_merge_approval` thì instance eval chấm rồi đóng ticket |
| Hạn mức | chung | trần riêng theo tuần; chỉ chạy khi `aico-ops safe` (hệ rảnh) |

Cách cô lập (đã làm, sau review độc lập):
- Ticket chạy lại = nhãn `eval` **và** artifact `eval` (một vị từ dùng chung cho claim, triage,
  builder, chỉ số). Nhãn một mình là ticket thường; API, intake GitHub và Jira bỏ nhãn `eval`.
- Instance `aico-eval` (`AICO_EVAL_INSTANCE=1`, `AICO_EVAL_VARIANT`, 1 ticket một lúc, không bản
  xem trước) chỉ nhận ticket eval của đúng biến thể; aico chính bỏ qua mọi ticket eval.
- Không merge, bốn lớp: graph luôn escalate ở bước duyệt merge; API từ chối `approved`;
  `verify_merge_approval` trả sai; mcp-gitlab chỉ merge vào `GITLAB_MERGE_TARGETS` (main).
- mcp-gitlab ép cặp: nguồn `eval/*` chỉ mở MR vào `eval/base/*`, nguồn khác không vào `eval/*`;
  không push vào `eval/base/*`; `publish` từ chối MR có sẵn trỏ sai đích.
- Planner/BA đọc code ở commit gốc (`read_file` kèm `ref`), không đọc `main` (có thể đã chứa đáp án).
- Jira, retro, chỉ số, bộ chuẩn bỏ qua ticket eval.

Việc phải sửa (ước lượng L):

1. `Ticket`: thêm artifact `kind: eval` (`ref` = `<variant>@<base_sha>`); không cần migration
   (artifact là JSON) nhưng phải mở rộng `Literal` của `Artifact.kind`.
2. `TicketStore.claim(owner, lease, *, labels_in / labels_not_in)`: lọc theo nhãn (SQL + bản
   in-memory), có test.
3. `builder.py`: đọc artifact `eval` để chọn branch gốc/đích; bỏ bước duyệt kế hoạch; dừng ở
   chờ merge.
4. `aico eval run --suite eval/tickets --variant <thư mục company> [--repeat 2]`: tạo ticket eval
   qua API, chờ xong, ghi `rnd_eval_runs` (hoặc file JSON trong `document/rnd/experiments/`).
5. Chấm: số liệu `ticket_metrics` + build/test trên máy build (`aico-remote`, lệnh trong
   `acceptance`) + kích thước diff so với `reference_sha` (chỉ tham khảo).
6. Dọn: xoá branch `eval/*` sau khi chấm (giữ diff trong bản ghi).

## 4. Đo và kết luận

- Mỗi biến thể chạy cùng bộ chuẩn **ít nhất 2 lần** (agent không tất định); so với baseline
  (`company/` hiện tại) chạy cùng tuần.
- Chỉ số chính: tỷ lệ đạt acceptance, tỷ lệ đạt vòng đầu, vòng review, token, phút. Chỉ số
  chặn: tỷ lệ Reviewer cho đạt mà acceptance hỏng (Reviewer dễ dãi) — xấu đi là loại biến thể.
- Bản ghi thí nghiệm `document/rnd/experiments/NNNN-<slug>.md`: giả thuyết và tiêu chí bác bỏ
  (viết **trước** khi chạy), diff biến thể, cỡ mẫu, kết quả, kết luận
  `confirmed / refuted / inconclusive`. Kết quả âm vẫn lưu.
- Với 10–15 ticket chỉ thấy được khác biệt lớn; ghi rõ cỡ mẫu, không ngoại suy.

## 5. Chi phí và an toàn

- Một lượt bộ chuẩn ≈ 15 ticket × (plan + 1–3 vòng code/review). Trần mặc định: 1 lượt/biến
  thể/tuần, dừng khi phòng nào vượt 70% hạn mức 5 giờ hoặc 60% tuần.
- Instance eval không có token merge; branch `eval/*` không bao giờ vào `main`.
- Đề bài lấy từ ticket cũ là dữ liệu, không phải lệnh (giữ nguyên bọc `<ticket>`).

## 6. Câu hỏi cho owner

1. Đồng ý chạy lại trên repo thật với branch `eval/*` (dọn sau khi chấm), hay muốn fork riêng
   (`VX-Anh/<repo>-eval`, phải cấp quyền token mới)? Mặc định đề xuất: branch `eval/*`.
2. Trần hạn mức cho eval mỗi tuần? Mặc định đề xuất: như mục 5.
3. Thí nghiệm đầu tiên: "chèn `<knowledge>` có giảm vòng review không" (đã có cờ `knowledge`
   trong event `job` để so ngay trên ticket thật trước khi cần chạy lại). Đồng ý?
