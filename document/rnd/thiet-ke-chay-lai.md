# Thiết kế R1: chạy lại ticket cũ để đo thay đổi của aico

Trạng thái (2026-11-12): **đã code trên nhánh `rnd-r1`** (tách từ `rnd-r0`), theo mặc định ở
mục 6 (branch trên repo thật, không fork). Đã merge vào `main` và deploy (2026-11-12).
Chưa chạy thật: cần bật `aico-eval`. Các lỗi review độc lập tìm ra được sửa ở nhánh `rnd-r1-fix`
(mục "Sửa sau review độc lập" bên dưới), chưa merge.

**Tên trung tính (2026-11-12):** agent có thể đổi hành vi khi biết mình bị chấm, nên ticket chạy
lại không lộ dấu hiệu nào trong prompt: nhánh `ai/<id>` như ticket thường, đích `base/<id>`, tiêu
đề giữ nguyên, nhãn `eval` bị bỏ khỏi khối `<ticket>`. Nhãn và artifact `eval` chỉ ở ticket store
để runner lọc.

**Cách dùng (sau khi merge + deploy):**

```bash
# 1. Bộ chuẩn: lấy ứng viên, chọn 10–15 mục, lưu suite.json (giữ trường base_sha, observed)
aico-ops bench 90 > suite.json
# 2. Biến thể: thư mục company/ đã sửa (prompt, workers, limits), đặt trong .env
#    AICO_EVAL_COMPANY_DIR=/đường/dẫn/biến-thể   (để trống = company/ hiện tại = baseline)
docker compose --profile eval up -d aico-eval
# 3. Chạy: mỗi mục 2 lần
aico-ops eval-start baseline 2 < suite.json
# 4. Xem, chấm acceptance (mục có acceptance: {build, test}), rồi đóng các ticket đã chấm
aico-ops eval-report baseline < suite.json
aico-ops eval-grade baseline < suite.json
aico-ops eval-report baseline --close < suite.json
# 5. Dọn nhánh ai/<id>, base/<id> của ticket đã đóng (xem trước bằng --dry-run). Kèm bộ chuẩn
#    để lượt của mục có acceptance mà chưa chấm được giữ lại (không kèm: mọi lượt chưa chấm đều giữ)
aico-ops eval-cleanup baseline --dry-run < suite.json
aico-ops eval-cleanup baseline < suite.json
```

Đã làm: nhãn + artifact `eval`, `workplan.ticket_refs` (nhánh `ai/<id>` từ commit gốc, MR vào
`base/<id>`), `TicketStore.claim(eval_only, eval_variant)`, `AICO_EVAL_INSTANCE`,
`AICO_EVAL_VARIANT`, bỏ duyệt kế hoạch và chặn merge trong code (không theo luật YAML),
mcp-gitlab/guard nhiều mẫu nhánh và ref SHA, `aico eval start|report`, service `aico-eval`
(profile `eval`), chỉ số chung không tính ticket eval.

Bổ sung trên nhánh `rnd-r1-gaps` (2026-11-12, chờ review + merge):
- `base_sha` của bộ chuẩn là merge-base: tool L0 `gitlab.merge_base` (GitHub compare API,
  GitLab `repository/merge_base`), cấp cho Reviewer (vai trò `/rnd/bench` đang dùng để đọc MR).
  `bench` so `base_sha` của PR với `head_sha` (không so với `main`: sau merge `main` đã chứa
  nhánh). Khác nhau thì giữ giá trị cũ ở `pr_base_sha`; `base_sha_source` = `merge_base` | `pr`
  (`pr`: không đọc được merge-base, lý do ở `base_sha_note`). Thư mục biến thể chép từ
  `company/` cũ không cần quyền này (bench chạy ở aico chính).
- Dọn nhánh: `aico eval cleanup --variant <biến thể> [--dry-run]` (`aico-ops eval-cleanup`). Với
  ticket chạy lại đã đóng hẳn (escalated/failed/done, không còn quyết định chờ xử lý) gọi tool
  L2 `gitlab.delete_branch_pair` bằng vai trò hệ thống `housekeeper` (vai trò duy nhất được cấp;
  guard ép `ai/*` + repo, có audit kèm SHA trước khi xoá). Server chỉ nhận tên `ai/<id>`, suy ra
  `base/<id>`, và chỉ xoá khi `base/<id>` tồn tại: nhánh `ai/*` của ticket thường, `main`, đích
  merge không bao giờ bị xoá; từ chối khi `ai/<id>` có MR mở vào đích khác. Xoá `ai/<id>` trước
  (không có lúc nào nhánh làm việc tồn tại mà thiếu nhánh gốc); lỗi giữa chừng thì chạy lại xoá
  nốt. Dọn xong ghi event `eval_cleanup`. Diff vẫn xem được ở PR đã đóng trên GitHub (GitHub tự
  đóng PR khi nhánh nguồn bị xoá); chưa chép diff vào bản ghi thí nghiệm. Dọn **sau** khi chấm
  acceptance (chấm cần nhánh `ai/<id>`).
- Trần hạn mức (`aico.rnd.evalgate`): runner của instance eval chỉ nhận ticket **mới** (trạng thái
  new) khi (1) số ticket chạy lại đã bắt đầu trong 7 ngày, mọi biến thể, còn dưới
  `AICO_EVAL_WEEKLY_CAP` (mặc định 40; 0 = không chạy ticket mới), (2) không ticket thường nào
  đang chạy hay chờ runner xử lý quyết định, (3) không phòng nào có agent và mọi cửa sổ hạn mức
  dưới `AICO_EVAL_MAX_SESSION_PERCENT` (70) / `AICO_EVAL_MAX_WEEK_PERCENT` (60) — hàm
  `rooms_free` dùng chung với bộ lập lịch dò công nghệ. Ticket đã bắt đầu vẫn chạy tiếp
  (`claim(skip_new=True)`), kể cả quyết định `--close`. Lý do chưa chạy ghi log của `aico-eval`
  (`chạy lại: chưa nhận ticket mới (...)`, chỉ khi lý do đổi); kết quả "chưa" giữ 60 giây. Cổng
  không dừng ticket đang chạy dở khi hạn mức vượt ngưỡng giữa chừng.
- Chấm acceptance: `aico eval grade --variant <biến thể> --suite suite.json [--regrade]`
  (`aico-ops eval-grade`). Mục bộ chuẩn có `acceptance: {build, test, image?}`; với ticket chạy
  lại đã tới chờ duyệt merge (kể cả đã đóng nhưng chưa dọn nhánh), aico tải archive nhánh
  `ai/<id>` qua gateway (vai trò coder, có audit) và chạy từng lệnh trên máy build
  (`RemoteExec` -> `POST /exec`, container dùng một lần, không mạng), build hỏng thì không chạy
  test. Kết quả ghi event `eval_acceptance` (`passed`, exit code, 2.000 ký tự cuối của log);
  lỗi hạ tầng (máy build tắt, exit 125/255) không tính là hỏng, chạy lại sẽ chấm tiếp. Agent
  không thấy lệnh hay kết quả (chấm sau khi agent xong, không đổi trạng thái ticket).
  `eval report` thêm `acceptance_graded`, `acceptance_pass_rate` (đạt / số lượt đã chấm),
  `acceptance_ungraded` và chỉ số chặn `review_pass_acceptance_fail_rate` (review đạt mà
  acceptance hỏng); `report --close` kèm bộ chuẩn không đóng lượt có acceptance mà chưa chấm
  (`close_skipped_ungraded`).

Sửa sau review độc lập, nhánh `rnd-r1-fix` (2026-11-12, chờ review + merge; các đoạn mô tả ở
trên đã cũ ở những điểm này):
- **`base/<id>` do vai trò hệ thống tạo, không phải agent.** Vai trò `coder` chỉ còn mẫu nhánh
  `ai/*`; builder tạo `base/<id>` bằng `housekeeper` (trước khi coder tạo `ai/<id>`). mcp-gitlab
  `create_branch base/<x>` chỉ nhận ref là SHA đủ 40 ký tự và từ chối khi `ai/<x>` đã tồn tại (gọi
  lại khi `base/<x>` đã có thì trả `created: false`). Lý do: `base/<x>` là dấu hiệu "nhánh `ai/<x>`
  không vào `main` và xoá được"; agent tạo được nó thì chặn được ticket thường vào `main` và làm
  `delete_branch_pair` xoá nhầm nhánh đang làm. `housekeeper` nay có `create_branch`,
  `branch_head`, `download_archive`, `delete_branch_pair`, mẫu `ai/*,base/*`. `base_sha` của bộ
  chuẩn bắt buộc là SHA đủ 40 ký tự (`eval start` từ chối tên nhánh). **Thư mục biến thể
  (`AICO_EVAL_COMPANY_DIR`) chép từ `company/` cũ phải cập nhật `roles/housekeeper.yaml` và
  `roles/coder.yaml`**, nếu không bước tạo `base/<id>` bị guard từ chối và ticket escalate.
- **Lỗi hạ tầng của máy build là trường riêng.** `POST /exec` trả `infra_error`; runner bọc lệnh
  để sh trong container in `::aico-exit <chuỗi ngẫu nhiên> <mã>` khi lệnh chạy xong, và in
  `::aico-infra ...` khi docker volume/network/container setup hỏng. Có dòng `::aico-exit` thì mã
  thoát là của lệnh (kể cả 125, 255) và được ghi là kết quả acceptance; trước đây 125/255 của
  chính lệnh bị coi là lỗi hạ tầng nên lượt không bao giờ được chấm. Không có dòng nào: 125 =
  docker không khởi động container, 255 qua SSH = mất kết nối. Hết slot trả 503 kèm
  `infra_error`. Lệnh `aico-remote` của worker không đổi (trường thừa, log đã bỏ dòng đánh dấu).
  `RemoteExec` gặp máy build bản cũ (thiếu trường) vẫn đoán theo 125/255 như trước.
- **build và test chạy trong một container.** `RemoteExec` gửi một lệnh gộp (`steps_script`): mỗi
  bước trong subshell `set -e`, có dòng `::aico-step <chuỗi ngẫu nhiên> <bước> start|exit <mã>` để
  tách log, mã thoát, thời gian từng bước (`split_steps`); bước hỏng thì dừng. Một volume, một lần
  cài dependency: test thấy sản phẩm của build. Thời hạn (`timeout_seconds`, mặc định 1.200 giây)
  nay tính cho cả lần chạy, không phải từng bước. Cài dependency hỏng hoặc hết giờ trước bước đầu
  tính cho bước đầu.
- **Chấm trên một commit, bằng vai trò hệ thống.** `eval grade` đọc SHA đầu nhánh `ai/<id>`
  (`branch_head`), tải archive theo SHA đó, ghi `sha` vào event `eval_acceptance`; gọi bằng
  `housekeeper`, không mượn quyền `coder`.
- **`eval cleanup` chỉ dọn lượt đã đóng thật.** Đã đóng = `done`/`failed`, hoặc `escalated` mà lần
  đổi trạng thái gần nhất là chờ duyệt merge -> escalated do quyết định `rejected` của actor
  `eval` (`report --close`). Ticket escalated vì lý do khác (hết vòng review, người từ chối, lỗi
  Git) có thể được xử lý tiếp nên giữ nhánh; bỏ hẳn thì `--escalated`. Lượt đã tới chờ duyệt merge
  mà chưa có `eval_acceptance` cũng giữ (kèm bộ chuẩn: chỉ mục có `acceptance`), trừ `--force`.
  Event `eval_cleanup` chỉ có hiệu lực khi mới hơn lần đổi trạng thái gần nhất: ticket chạy tiếp
  sau khi dọn không bị khoá ở "đã dọn" (áp dụng cho cả `grade`).
- **Cổng hạn mức không bị hai runner vượt cùng lúc.** Runner giữ `EvalGate.lock` suốt đoạn hỏi cổng
  + claim; ticket `new` đang có lease được đếm là đã bắt đầu; kết quả "được" giữ 10 giây và mất
  ngay khi một runner nhận ticket mới.
- Thông báo `push` vào `base/*` trả cho agent chỉ còn "không push vào base/*" (bản cũ lộ chữ
  "lượt chạy lại").

Còn thiếu sau nhánh này:
- Ticket chạy lại escalated giữa chừng (không tới chờ duyệt merge) không có lệnh đóng riêng:
  `eval cleanup --escalated` dọn mọi ticket escalated của biến thể, owner phải tự chắc không còn
  lượt nào định chạy tiếp.
- Dòng đánh dấu của `/exec` nằm trong tham số của sh trong container, nên mã của bài làm đọc được
  qua `/proc/1/cmdline` và in được dòng giả. Dòng thật in sau cùng và mã thoát lấy từ `docker
  run`, nên giả chỉ đổi được phân loại hạ tầng trong trường hợp container chết bất thường; chưa
  xử lý vì lợi ích cho agent gần như không có (chỉ làm lượt không được chấm).
- Chưa chạy thật `eval grade` trên máy build (mới test với máy build giả): cần thử image mặc định
  có đủ công cụ cho từng repo và thời gian cài dependency.
- Lệnh acceptance chạy trên nhánh của agent, nên agent sửa `package.json`/file test là tự làm
  đề dễ đi. Cần chép file test từ `reference_head_sha` đè lên trước khi chạy (chưa làm).
- Chỉ chấm lượt đã qua review; lượt escalate giữa chừng không chấm (đã tính trượt ở `pass_rate`).
- Chưa có bản ghi thí nghiệm tự sinh (`document/rnd/experiments/`), tập `holdout`, so kích thước
  diff với `reference_sha`, chép diff vào bản ghi trước khi dọn nhánh.
- Thứ tự đúng vẫn là report -> grade -> report --close -> cleanup; `cleanup` nay tự giữ lượt
  chưa chấm (xem trên).
- Cổng hạn mức đếm mọi agent ở các phòng, không phân biệt agent của chính instance eval: instance
  eval nên giữ `AICO_RUNNER_CONCURRENCY=1`.

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
| Branch làm việc | `ai/<ticket>` từ `main` | `ai/<ticket>` từ `base_sha` (tên như ticket thường) |
| Đích của MR | `main` | `base/<ticket>` (branch tạo ở `base_sha`) — diff đúng phần agent làm |
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
- mcp-gitlab ép cặp: `base/<x>` chỉ nhận MR từ đúng `ai/<x>`; `ai/<x>` có nhánh `base/<x>` thì không
  mở MR hay merge vào `main` (kiểm trên repo, không dựa vào nhãn); không push vào `base/*`;
  `publish` từ chối MR có sẵn trỏ sai đích.
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
6. Dọn: xoá branch `ai/<id>`, `base/<id>` của ticket chạy lại sau khi chấm (giữ diff trong bản ghi).

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
- Instance eval không có token merge; nhánh của ticket chạy lại không bao giờ vào `main`.
- Đề bài lấy từ ticket cũ là dữ liệu, không phải lệnh (giữ nguyên bọc `<ticket>`).

## 6. Câu hỏi cho owner

1. Đồng ý chạy lại trên repo thật với branch riêng (dọn sau khi chấm), hay muốn fork riêng
   (`VX-Anh/<repo>-eval`, phải cấp quyền token mới)? Mặc định đề xuất: branch riêng (đã làm).
2. Trần hạn mức cho eval mỗi tuần? Mặc định đề xuất: như mục 5.
3. Thí nghiệm đầu tiên: "chèn `<knowledge>` có giảm vòng review không" (đã có cờ `knowledge`
   trong event `job` để so ngay trên ticket thật trước khi cần chạy lại). Đồng ý?
