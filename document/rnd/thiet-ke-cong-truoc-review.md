# Cổng trước review

Kiểm tất định (không gọi model) nhánh `ai/<ticket>` giữa bước `code` và `review` của SOP
`fix_ticket`. Mã: `src/aico/orchestrator/prereview.py` (hàm thuần) và node `gate` trong
`builder.py`. Test: `tests/test_prereview_gate.py`, `tests/test_prereview_checks.py` (kiểm tra
`checks`).

## Vì sao

Đọc ghi chú review của khoảng 80 ticket (linh-hoa-vien, ai-trace): lớp lỗi lớn nhất là lỗi quy
trình mà máy thấy được trước khi tốn một vòng review:

- diff xoá file hoặc việc đã merge của người khác (T-fbf4a009 xoá `src/core/alchemy/`,
  T-0cc6f4f1 xoá `examples/shop-ai/**`);
- sửa file ngoài `owns` của đầu việc;
- nộp review khi bảng đầu việc còn dòng lỗi/bỏ qua (worker hết hạn mức);
- bỏ tick checklist trong `docs/delivery/sprint-*.md` (đặc thù linh-hoa-vien, xem "Chưa làm");
- build/test/typecheck đỏ lúc nộp review: khoảng 37 nhận xét (linh-hoa-vien ~22, ai-trace 12,
  aico-office 3; T-4fd89527 typecheck đỏ 4 vòng). Kiểm tra `checks` chạy đúng các lệnh đó trước
  khi tốn vòng review.

## Luồng

```text
code ──> gate ──đạt──────────────> review ──> test ──> duyệt merge
          │ vi phạm, còn lượt
          ├──────────────> code (vòng sửa với góp ý của cổng)
          │ vi phạm, hết lượt
          └──────────────> escalate ("cổng trước review: ...")
```

Ticket vẫn ở `in_progress` trong lúc qua cổng; mỗi lần kiểm ghi một event `gate`.

## Mức của từng kiểm tra

`deletions`, `outside_owns`, `unfinished_items`, `checks` mỗi cái một mức:

- `off`: không kiểm.
- `warn` (mặc định): ghi vi phạm vào event `gate` (`violations` có `mode: warn`, tên kiểm tra nằm
  trong `would_block`) rồi **vẫn cho tới review**; coder không nhận góp ý nào của cổng.
- `block`: trả về bước code như mô tả ở "Khi vi phạm".

Mặc định `warn` để đo tỷ lệ chặn nhầm trên ticket thật: so `gate.would_block` ở `/rnd/metrics` với
ghi chú review của chính các ticket đó, rồi owner mới bật `block` cho từng kiểm tra.

## Kiểm tra

Diff tính giữa **commit chung** của nhánh làm việc với nhánh đích (`merge_base`) và đầu nhánh làm
việc, bằng hai lần `download_archive` (so băm nội dung, tính cả file nhị phân). Không so với đầu
nhánh đích: việc người khác merge sau khi nhánh tách ra không bị tính là "nhánh này xoá".

| Khoá | Vi phạm khi | Không chặn khi |
| --- | --- | --- |
| `deletions` | file có ở commit chung, mất ở nhánh | file nằm trong `owns`; là đổi tên/chuyển chỗ (có file thêm mới cùng nội dung, hoặc cùng tên ở thư mục kề: mỗi bên lệch tối đa một cấp); đường dẫn hay thư mục chứa nó được nêu trong ticket, kế hoạch hoặc góp ý |
| `outside_owns` | file thêm/sửa không khớp `owns` của đầu việc nào | kế hoạch không chia `owns` (có đầu việc `**`: bỏ cả kiểm tra); file do hệ thống ghi (`docs/plans/<id>.md`, `docs/architecture.md`); đường dẫn được nêu trong góp ý; file đi kèm: lockfile (`package-lock.json`, `pnpm-lock.yaml`, `uv.lock`...), file test và snapshot (`tests/`, `__tests__/`, `*.test.*`, `*.spec.*`, `test_*.py`, `*.snap`...), file cùng thư mục cùng tên gốc với một file trong `owns` mà nhánh đã đổi (`hero.png` cạnh `hero.pixel.json`) |
| `unfinished_items` | bảng đầu việc còn dòng khác `xong` (bảng giữ qua các vòng sửa `FIX`) | đầu việc đã được chạy lại mà vẫn "không sửa file nào trong phạm vi": ghi chú, để reviewer xét |
| `stale_base` | commit chung khác đầu nhánh đích | luôn chỉ ghi chú (`stale_base: true` + `notes` trong event), không chặn |

### `checks`: build/test/typecheck của repo

Vi phạm khi một bước kiểm của repo exit khác 0 trên **đầu nhánh `ai/<id>`**. Không gọi model.

- Lệnh khai theo repo ở `prereview_gate.check_projects` (xem "Cấu hình"). Repo không có ở đó:
  bỏ kiểm tra, event không có khoá `checks`.
- Mỗi lượt qua cổng **một** lần `POST /exec` tới máy build (`RemoteExec`, vai trò của bước code):
  ghim SHA đầu nhánh (`branch_head`), tải archive theo SHA đó, chạy các bước nối tiếp trong một
  container (`steps_script`), dừng ở bước hỏng đầu tiên. Máy build tự cài dependency theo lockfile
  ở pha chuẩn bị (có mạng); các bước chạy không có mạng, nên cấu hình không ghi lệnh cài.
- Kết quả trong event `gate`, khoá `checks`: `sha`, `passed`, `mode`, `duration_seconds` (cả lần
  chạy, tính cả chờ slot và cài dependency), `failed_step`, `steps` (`name`, `exit_code`,
  `duration_seconds`; bước hỏng thêm `tail` là 1.500 ký tự cuối của log, đã bỏ mã màu).
- `warn`: `checks` nằm trong `would_block`, vẫn tới review. `block`: trả về bước code (đầu việc
  `FIX`) với tên bước, lệnh, mã thoát và đuôi log (80 dòng / 4.000 ký tự cuối); dùng chung
  `gate_rounds`/`max_rounds` với các kiểm tra khác.
- **Không chặn** khi không kết luận được, kể cả ở mức `block`; event ghi `checks_skipped: true` và
  lý do trong `notes`: máy build báo `infra_error`, không phản hồi, hết slot (HTTP 503), không tải
  được mã nguồn, hết giờ (máy build trả exit 124 không kèm log bước: không phân biệt được bài làm
  treo với máy chậm), hoặc log thiếu dòng đánh dấu bước.
- Không chạy cho ticket không phải code: nhãn trong `checks_skip_labels` (mặc định `research`,
  `docs`, `retro`), nhãn có phạm vi riêng (`label_scopes`), nhãn bỏ bước test của SOP. Ba kiểm tra
  kia vẫn chạy như cũ.
- Không có máy build (`AICO_REMOTE_URL` trống hoặc `/health` không đáp lúc khởi động): kiểm tra
  không chạy, không ghi gì.
- Ticket chạy lại của R&D: chạy như ticket thường trên `ai/<id>`; góp ý chỉ có tên bước, lệnh và
  log.

**Chi phí.** Mỗi lượt qua cổng tốn một lần cài dependency + build + test trên máy build (không
tốn token), và node `gate` chờ nó xong: review bắt đầu muộn đúng bằng thời gian đó, cộng tối đa
600 giây chờ slot khi máy build bận. Giới hạn: `checks_timeout` (mặc định 900 giây, máy build kẹp
ở `REMOTE_MAX_TIMEOUT` = 1.200) hoặc `timeout` riêng của repo; kết nối HTTP cắt sau giới hạn + 120
giây. Vòng sửa nào cũng chạy lại (chưa nhớ kết quả theo SHA). Coder, Reviewer và Tester vẫn tự
chạy build/test bằng `aico-remote`: cổng là lần chạy thêm, đổi lấy việc nhánh đỏ không tới
reviewer.

**Chỉ xét phần mới.** Diff cộng dồn cả nhánh, nên cổng nhớ hai thứ trong state:

- `gate_accepted`: mọi file (đổi hoặc xoá) đã có trên nhánh ở lần gần nhất nhánh được gửi review.
  Hai kiểm tra theo diff bỏ qua các file này: thứ reviewer đã thấy không bị đòi hoàn tác ở vòng sau.
- `gate_feedback_log`: mọi góp ý ticket từng nhận (review, kiểm thử, người duyệt merge, merge lỗi),
  không chỉ của vòng gần nhất.

Ticket đang chạy dở lúc deploy không có `gate_accepted` trong state: lần kiểm đầu nhận hết diff đang
có trên nhánh (ghi chú trong event), từ đó chỉ xét thay đổi mới.

`owns` là hợp `owns` của mọi đầu việc trong kế hoạch. Ticket có nhãn mang phạm vi riêng
(`label_scopes`, vd `research` chỉ `cards/`) thì `owns` bị kẹp vào phạm vi đó như ở bước code, nên
luồng research/docs/retro đi qua cổng mà không cần cấu hình thêm; muốn tắt hẳn cho một nhãn thì
thêm vào `skip_labels`.

Ticket chạy lại của R&D: đích là `base/<id>` dựng ở đúng commit gốc nên không bao giờ "gốc cũ";
góp ý của cổng chỉ nhắc tên nhánh trung tính, không nói ticket đang được chấm.

Lỗi hạ tầng (không tìm được commit chung, tool Git lỗi, bị từ chối quyền) không chặn: bỏ hai kiểm
tra theo diff, ghi lý do vào `notes` của event, vẫn kiểm bảng đầu việc.

## Khi vi phạm

Phần này chỉ áp cho kiểm tra ở mức `block`.

- Quay lại `code` với góp ý liệt kê từng file và lý do, đi theo đường của vòng sửa sau review
  (đầu việc `FIX`). Khi chỉ còn vi phạm đầu việc chưa xong thì chạy lại đúng các đầu việc đó với
  `owns` cũ. Vi phạm hỗn hợp: sửa file trước bằng `FIX`, đầu việc dở vẫn nằm trong bảng và được
  chạy lại ở lần trả về sau. Bảng đầu việc trong `docs/plans/<id>.md` gộp kết quả các lần.
- Đầu việc chạy lại mà agent vẫn không sửa gì (thấy không cần sửa, hoặc `owns` sai): không
  escalate, nhánh giữ nguyên và đi tới review kèm ghi chú. Lỗi worker thì vẫn tính là chưa xong.
- Bộ đếm riêng `gate_rounds` đếm số lần trả về **liên tiếp**, về 0 khi cổng cho qua. Quá
  `max_rounds` thì escalate với lý do bắt đầu bằng `cổng trước review:`; event escalated kèm
  `gate_feedback`. Không dùng chung `review_rounds`: cổng không ăn vào số vòng của reviewer.

## Cấu hình

`company/sops/fix_ticket.yaml` (thiếu khoá thì dùng đúng các mặc định này):

```yaml
prereview_gate:
  enabled: true
  deletions: warn         # off | warn | block
  outside_owns: warn      # off | warn | block
  unfinished_items: warn  # off | warn | block
  stale_base: warn        # off | warn
  checks: warn            # off | warn | block
  checks_timeout: 900     # giây cho cả lần chạy
  checks_skip_labels: [research, docs, retro]
  check_projects: {}      # repo -> {steps: [{name, run}], image?, timeout?}
  max_rounds: 2
  skip_labels: []
```

Lệnh kiểm đang khai trong `company/sops/fix_ticket.yaml`:

| Repo | Bước (theo thứ tự) | Nguồn |
| --- | --- | --- |
| `VX-Anh/aico-office` | `npm run typecheck`, `npm run build`, `npm test` | `package.json`, AGENTS.md và `.github/workflows/ci.yml` của repo |
| `VX-Anh/ai-trace` | `pnpm -r build`, `pnpm -r typecheck`, `pnpm test` | AGENTS.md của repo (build trước vì typecheck cần `dist/` của `@ai-trace/model`) |
| `VX-Anh/linh-hoa-vien` | `npm run typecheck`, `npm run build`, `npm test` | `package.json` và bảng lệnh trong AGENTS.md của repo; không chạy `test:e2e`/`test:perf` (Playwright, chậm) |

Tên bước: chữ thường, số, `-`, `_` (nằm trong dòng đánh dấu của log), không trùng nhau. `image` là
tên trong `REMOTE_IMAGES` của máy build (bỏ trống: `node`).

YAML đọc `off` trần thành `false`: spec nhận `false` là `off`, còn `true`/`on` thì báo lỗi cấu hình
(phải ghi rõ mức).

Vai trò `coder` được cấp thêm tool chỉ đọc `gitlab.merge_base` (bước code gọi tool Git bằng vai
trò này).

## Số đo

- Event `gate`: `passed` (không có vi phạm mức block), `attempt`, `round`, `stale_base`,
  `diff_skipped` (bỏ kiểm diff vì lỗi Git), `violations` (`check`, `mode`, `count`, `paths` tối đa
  40), `would_block` (kiểm tra mức warn có vi phạm), `notes`, `feedback` (khi trả về), `escalated`,
  `checks` và `checks_skipped` (xem mục `checks`).
- `/rnd/metrics`: mỗi ticket có `gate_checks`, `gate_blocks`, `gate_stale_base`, `gate_skipped`,
  `gate_violations` (mức block), `gate_would_block` (mức warn); phần gộp có `gate` (`checks`,
  `blocks`, `tickets_blocked`, `stale_base`, `skipped`, `violations`, `would_block` theo kiểm tra,
  `tickets_would_block`). Lớp escalate mới: `cổng trước review`.
- Kiểm tra `checks`: số lần chặn / lẽ ra chặn nằm ở `violations.checks` / `would_block.checks`.
  Thêm cho mỗi ticket `gate_ci_runs`, `gate_ci_failed`, `gate_ci_skipped`, `gate_ci_seconds`; phần
  gộp `gate.ci` có `runs`, `failed`, `skipped`, `seconds_total`, `seconds_avg`. Khác vi phạm về
  file, nhánh đỏ được đếm lại ở mỗi lượt qua cổng cho tới khi xanh.
- Ở mức warn, vi phạm về file chỉ đếm một lần (file vào `gate_accepted` khi nhánh tới review);
  đầu việc dở thì đếm lại mỗi lần kiểm cho tới khi xong.
- Lần trả về của cổng không đổi trạng thái ticket nên không tính vào `code_rounds`/`first_pass_rate`.

## Rủi ro chặn nhầm

- Xoá file có chủ đích mà ticket/kế hoạch không nêu đường dẫn (vd "dọn code chết") và file không
  nằm trong `owns`: bị trả về, agent không có cách "khai báo xoá" nên sau `max_rounds` sẽ escalate.
  Cách tránh: kế hoạch đưa file/thư mục cần xoá vào `owns` hoặc nêu đường dẫn.
- Reviewer yêu cầu sửa "ở chỗ khác" mà không nêu đường dẫn, và file đó không phải test,
  lockfile hay file đi kèm: vòng sửa đụng file ngoài `owns` bị trả về.
- Đầu việc mà agent thấy không cần sửa gì tốn thêm một lượt chạy lại trước khi tới review.
- Thư mục cấp một nêu trần (không có `/`) không được tính là đã nêu.
- `checks`: test chập chờn (flaky) hoặc test cần thứ máy build không có ở pha chạy (mạng, biến
  môi trường, dịch vụ ngoài) sẽ đỏ dù bài làm đúng. Nhánh đích vốn đã đỏ thì ticket nào cũng đỏ
  (cổng không so với kết quả của nhánh đích). Xem `would_block.checks` trước khi bật `block`.
- `checks` chạy lệnh trên nhánh của agent: agent sửa `package.json` hay file test là tự làm xanh
  được; cổng chỉ bắt nhánh đỏ, không thay reviewer.
- Miễn trừ file test là theo tên: file không phải test nằm trong thư mục `tests/` cũng lọt.

Các rủi ro trên chỉ thành chặn thật khi kiểm tra ở mức `block`; ở `warn` chúng hiện thành
`would_block` để đối chiếu.

## Chưa làm (đề xuất)

- **Bỏ tick checklist sprint** (`[x]` thành `[ ]` trong `docs/delivery/sprint-*.md`): đặc thù
  linh-hoa-vien, không đưa vào aico. Đề xuất: thêm kiểm tra vào CI/`npm test` của chính repo đó
  (so số dòng `- [x]` của file với nhánh đích, giảm thì fail), hoặc sau này cho cổng một danh sách
  luật theo repo khai báo trong `company/`.
- **Cập nhật nhánh khi gốc cũ**: Reviewer/Tester đã diff từ commit chung (nhánh
  `rnd-review-mergebase`), nên gốc cũ không còn hiện thành "xoá việc đã merge". Còn lại: khi cổng
  báo `stale_base`, hệ thống nên tự merge đích vào `ai/<id>`, vì agent ở worker không có Git để
  rebase.
- **`checks` nhớ theo SHA**: lượt qua cổng mà đầu nhánh không đổi (đầu việc chạy lại không sửa gì)
  vẫn build lại; có thể nhớ SHA đã xanh trong state. Chưa có e2e của linh-hoa-vien trong `checks`.
- **Tự khôi phục**: với `deletions`/`outside_owns`, hệ thống có thể tự đẩy commit trả file về bản
  gốc thay vì tốn một lượt agent.
