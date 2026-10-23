---
name: aico-ops
description: Thao tác có tác dụng thật lên stack aico trong WSL — deploy code mới, tạo ticket, duyệt/từ chối kế hoạch hoặc merge, xử lý ticket escalated (merge, retry_push).
disable-model-invocation: true
argument-hint: deploy | create <file.json> | decide <id> <approved|revise|changes_requested|rejected> [ghi chú] | escalate <id> <action> [ghi chú]
---

Thực hiện: $ARGUMENTS

```powershell
wsl -d Ubuntu -u aico -- bash /mnt/d/TVPL/Marshy/ai-company/deploy/aico-ops.sh <lệnh>
```

- `deploy`: tự chạy `safe` trước; đang có job thì dừng và báo người dùng ticket/agent nào đang chạy.
  Chỉ dùng `deploy --force` khi người dùng nói rõ chấp nhận cắt ngang job. Sau deploy, xem
  `docker compose ps` trong output: service nào không `Up` thì đọc `logs <service>`.
- `create <file.json>`: viết JSON ticket vào thư mục scratchpad, truyền đường dẫn dạng WSL
  (`C:\...` → `/mnt/c/...`). Trường: `title`, `body` (nội dung; field lạ bị bỏ qua), `project` (vd `VX-Anh/aico-office`), `labels`.
- `decide <id> <approved|revise|changes_requested|rejected> [ghi chú]`: quyết định của owner (L3). Chỉ đúng quyết định người
  dùng vừa nói cho đúng ticket đó; trước khi duyệt merge, tóm tắt PR và kết luận review cho họ.
- `escalate <id> <merge|retry_push> [ghi chú]`: ticket escalated. `retry_push` đẩy lại thay đổi
  chưa push và mở PR; xong vẫn ở escalated, người dùng xem PR rồi mới `merge`.

Xem trạng thái trước/sau bằng skill `aico-status` (`ticket <id>`).
