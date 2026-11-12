# Cổng trước review

Kiểm tất định (không gọi model) nhánh `ai/<ticket>` giữa bước `code` và `review` của SOP
`fix_ticket`. Mã: `src/aico/orchestrator/prereview.py` (hàm thuần) và node `gate` trong
`builder.py`. Test: `tests/test_prereview_gate.py`.

## Vì sao

Đọc ghi chú review của khoảng 80 ticket (linh-hoa-vien, ai-trace): lớp lỗi lớn nhất là lỗi quy
trình mà máy thấy được trước khi tốn một vòng review:

- diff xoá file hoặc việc đã merge của người khác (T-fbf4a009 xoá `src/core/alchemy/`,
  T-0cc6f4f1 xoá `examples/shop-ai/**`);
- sửa file ngoài `owns` của đầu việc;
- nộp review khi bảng đầu việc còn dòng lỗi/bỏ qua (worker hết hạn mức);
- bỏ tick checklist trong `docs/delivery/sprint-*.md` (đặc thù linh-hoa-vien, xem "Chưa làm").

## Luồng

```text
code ──> gate ──đạt──────────────> review ──> test ──> duyệt merge
          │ vi phạm, còn lượt
          ├──────────────> code (vòng sửa với góp ý của cổng)
          │ vi phạm, hết lượt
          └──────────────> escalate ("cổng trước review: ...")
```

Ticket vẫn ở `in_progress` trong lúc qua cổng; mỗi lần kiểm ghi một event `gate`.

## Kiểm tra

Diff tính giữa **commit chung** của nhánh làm việc với nhánh đích (`merge_base`) và đầu nhánh làm
việc, bằng hai lần `download_archive` (so băm nội dung, tính cả file nhị phân). Không so với đầu
nhánh đích: việc người khác merge sau khi nhánh tách ra không bị tính là "nhánh này xoá".

| Khoá | Vi phạm khi | Không chặn khi |
| --- | --- | --- |
| `deletions` | file có ở commit chung, mất ở nhánh | file nằm trong `owns`; là đổi tên/chuyển chỗ (có file thêm mới cùng nội dung hoặc cùng tên); đường dẫn hay thư mục chứa nó được nêu trong ticket, kế hoạch hoặc góp ý review |
| `outside_owns` | file thêm/sửa không khớp `owns` của đầu việc nào | kế hoạch không chia `owns` (có đầu việc `**`: bỏ cả kiểm tra); file do hệ thống ghi (`docs/plans/<id>.md`, `docs/architecture.md`); đường dẫn được nêu trong góp ý của reviewer/tester/người duyệt |
| `unfinished_items` | bảng đầu việc của lần code gần nhất còn dòng khác `xong` | vòng sửa `FIX` (một đầu việc, thay cho cả bảng) |
| `stale_base` | commit chung khác đầu nhánh đích | luôn chỉ ghi chú (`stale_base: true` + `notes` trong event), không chặn |

`owns` là hợp `owns` của mọi đầu việc trong kế hoạch. Ticket có nhãn mang phạm vi riêng
(`label_scopes`, vd `research` chỉ `cards/`) thì `owns` bị kẹp vào phạm vi đó như ở bước code, nên
luồng research/docs/retro đi qua cổng mà không cần cấu hình thêm; muốn tắt hẳn cho một nhãn thì
thêm vào `skip_labels`.

Ticket chạy lại của R&D: đích là `base/<id>` dựng ở đúng commit gốc nên không bao giờ "gốc cũ";
góp ý của cổng chỉ nhắc tên nhánh trung tính, không nói ticket đang được chấm.

Lỗi hạ tầng (không tìm được commit chung, tool Git lỗi, bị từ chối quyền) không chặn: bỏ hai kiểm
tra theo diff, ghi lý do vào `notes` của event, vẫn kiểm bảng đầu việc.

## Khi vi phạm

- Quay lại `code` với góp ý liệt kê từng file và lý do, đi theo đường của vòng sửa sau review
  (đầu việc `FIX`). Riêng khi vi phạm duy nhất là đầu việc chưa xong thì chạy lại đúng các đầu việc
  đó với `owns` cũ, bảng đầu việc trong `docs/plans/<id>.md` gộp kết quả hai lần.
- Bộ đếm riêng `gate_rounds` đếm số lần trả về **liên tiếp**, về 0 khi cổng cho qua. Quá
  `max_rounds` thì escalate với lý do bắt đầu bằng `cổng trước review:`; event escalated kèm
  `gate_feedback`. Không dùng chung `review_rounds`: cổng không ăn vào số vòng của reviewer.

## Cấu hình

`company/sops/fix_ticket.yaml` (thiếu khoá thì dùng đúng các mặc định này):

```yaml
prereview_gate:
  enabled: true
  deletions: true
  outside_owns: true
  unfinished_items: true
  stale_base: warn      # off | warn
  max_rounds: 2
  skip_labels: []
```

Vai trò `coder` được cấp thêm tool chỉ đọc `gitlab.merge_base` (bước code gọi tool Git bằng vai
trò này).

## Số đo

- Event `gate`: `passed`, `attempt`, `round`, `stale_base`, `violations` (`check`, `count`,
  `paths` tối đa 40), `notes`, `feedback` (khi vi phạm), `escalated`.
- `/rnd/metrics`: mỗi ticket có `gate_checks`, `gate_blocks`, `gate_stale_base`,
  `gate_violations`; phần gộp có `gate` (`checks`, `blocks`, `tickets_blocked`, `stale_base`,
  `violations` theo loại). Lớp escalate mới: `cổng trước review`.
- Lần trả về của cổng không đổi trạng thái ticket nên không tính vào `code_rounds`/`first_pass_rate`.

## Rủi ro chặn nhầm

- Xoá file có chủ đích mà ticket/kế hoạch không nêu đường dẫn (vd "dọn code chết") và file không
  nằm trong `owns`: bị trả về, agent không có cách "khai báo xoá" nên sau `max_rounds` sẽ escalate.
  Cách tránh: kế hoạch đưa file/thư mục cần xoá vào `owns` hoặc nêu đường dẫn.
- Reviewer yêu cầu sửa "ở chỗ khác" mà không nêu đường dẫn: vòng sửa đụng file ngoài `owns` bị trả về.
- Đầu việc mà agent thấy không cần sửa gì ("không sửa file nào trong phạm vi") bị coi là chưa
  xong và chạy lại; trước đây đi thẳng tới review.
- Thư mục cấp một nêu trần (không có `/`) không được tính là đã nêu.

Tắt từng kiểm tra bằng cờ tương ứng nếu số `gate.blocks` cho thấy chặn nhầm.

## Chưa làm (đề xuất)

- **Bỏ tick checklist sprint** (`[x]` thành `[ ]` trong `docs/delivery/sprint-*.md`): đặc thù
  linh-hoa-vien, không đưa vào aico. Đề xuất: thêm kiểm tra vào CI/`npm test` của chính repo đó
  (so số dòng `- [x]` của file với nhánh đích, giảm thì fail), hoặc sau này cho cổng một danh sách
  luật theo repo khai báo trong `company/`.
- **Diff của Reviewer so hai đầu nhánh**: `CliReviewer` so `source_branch` với đầu `target_branch`.
  Khi nhánh dựng trên gốc cũ, việc người khác đã merge hiện ra như bị nhánh xoá. Nhiều ghi chú
  "xoá việc đã merge" có thể đến từ đây chứ không phải agent xoá thật. Đề xuất: Reviewer cũng diff
  từ `merge_base`, và hệ thống tự cập nhật nhánh (merge đích vào `ai/<id>`) khi cổng báo
  `stale_base`, vì agent ở worker không có Git để rebase.
- **Tự khôi phục**: với `deletions`/`outside_owns`, hệ thống có thể tự đẩy commit trả file về bản
  gốc thay vì tốn một lượt agent.
