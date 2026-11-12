"""Cổng trước review: kiểm tất định (không gọi model) nhánh làm việc trước khi tốn một vòng review.

Bắt lớp lỗi quy trình mà máy thấy được: xoá file không thuộc phạm vi đầu việc, sửa/thêm file
ngoài `owns`, nộp review khi còn đầu việc lỗi/bỏ dở. Diff tính so với commit chung của nhánh
làm việc và nhánh đích (không so với đầu nhánh đích), nên việc người khác merge sau khi nhánh
tách ra không bị coi là "bị xoá". Nhánh dựng trên gốc cũ chỉ được ghi chú.

Module này chỉ có hàm thuần; node `gate` trong builder lo gọi tool Git và ghi event.
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
import tarfile
from dataclasses import dataclass, field
from typing import Any

from aico.orchestrator.workplan import ALL_FILES, WorkItem

GATE_LABEL = "cổng trước review"
DELETIONS = "deletions"
OUTSIDE_OWNS = "outside_owns"
UNFINISHED_ITEMS = "unfinished_items"
MAX_LISTED = 40  # số file tối đa liệt kê trong góp ý và event


@dataclass(frozen=True)
class BranchDiff:
    """Thay đổi của nhánh so với commit chung với nhánh đích."""

    deleted: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()  # thêm mới hoặc đổi nội dung
    # File xoá thực ra là đổi tên/chuyển chỗ: có file thêm mới cùng nội dung hoặc cùng tên.
    moved: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Violation:
    check: str  # deletions | outside_owns | unfinished_items
    paths: tuple[str, ...] = ()  # file vi phạm, hoặc id đầu việc chưa xong
    details: tuple[str, ...] = ()  # dòng mô tả từng mục (đầu việc: id + lỗi)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "count": len(self.paths),
            "paths": list(self.paths[:MAX_LISTED]),
        }


@dataclass(frozen=True)
class GateResult:
    violations: tuple[Violation, ...] = ()
    notes: tuple[str, ...] = field(default=())

    @property
    def passed(self) -> bool:
        return not self.violations

    def only(self, check: str) -> bool:
        return bool(self.violations) and all(v.check == check for v in self.violations)


def archive_digests(archive_b64: str) -> dict[str, str]:
    """{đường dẫn: sha1 nội dung} mọi file trong tar.gz (bỏ thư mục gốc; tính cả file nhị phân)."""
    digests: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(archive_b64)), mode="r:gz") as tar:
        for member in tar.getmembers():
            parts = member.name.split("/", 1)
            if not member.isfile() or len(parts) < 2:
                continue
            raw = tar.extractfile(member)
            if raw is not None:
                digests[parts[1]] = hashlib.sha1(raw.read(), usedforsecurity=False).hexdigest()
    return digests


def branch_diff(base_b64: str, head_b64: str) -> BranchDiff:
    base, head = archive_digests(base_b64), archive_digests(head_b64)
    deleted = sorted(base.keys() - head.keys())
    added = sorted(head.keys() - base.keys())
    changed = sorted(p for p in head if base.get(p) != head[p])
    added_digests = {head[p] for p in added}
    added_names = {p.rsplit("/", 1)[-1] for p in added}
    moved = frozenset(
        p for p in deleted if base[p] in added_digests or p.rsplit("/", 1)[-1] in added_names
    )
    return BranchDiff(tuple(deleted), tuple(changed), moved)


def mentioned(path: str, text: str) -> bool:
    """`path` (hoặc thư mục chứa nó) có được nêu đích danh trong `text` không.

    Dùng để không chặn nhầm: file mà kế hoạch, ticket hay góp ý của reviewer/người duyệt đã nêu
    thì được coi là có chủ đích. Thư mục cấp một chỉ tính khi viết kèm dấu '/' (vd `legacy/`):
    chữ `src` trần xuất hiện ở khắp nơi, không nói lên điều gì.
    """
    if not text:
        return False
    parts = path.split("/")
    dirs = ["/".join(parts[:n]) for n in range(1, len(parts))]
    tail = r"(?![\w/-])(?!\.\w)"
    patterns = [rf"(?<![\w./-]){re.escape(path)}{tail}"]
    for d in dirs:
        slash = r"/(?:\*\*?)?" if "/" not in d else r"(?:/(?:\*\*?)?)?"
        patterns.append(rf"(?<![\w./-]){re.escape(d)}{slash}{tail}")
    return any(re.search(p, text) for p in patterns)


def declared_owns(items: list[WorkItem]) -> tuple[str, ...] | None:
    """Hợp các vùng file của mọi đầu việc; None khi không xác định (không có đầu việc, hoặc có
    đầu việc được sửa mọi file: ticket cũ, kế hoạch không chia đầu việc)."""
    if not items or any(ALL_FILES in item.owns for item in items):
        return None
    return tuple(dict.fromkeys(p for item in items for p in item.owns))


def _allows(owns: tuple[str, ...], path: str) -> bool:
    return WorkItem("_", "", owns=owns).allows(path)


def check_branch(
    diff: BranchDiff | None,
    items: list[WorkItem],
    item_results: list[dict[str, Any]],
    *,
    system_paths: tuple[str, ...] = (),
    intent_text: str = "",
    feedback_text: str = "",
    deletions: bool = True,
    outside_owns: bool = True,
    unfinished_items: bool = True,
) -> GateResult:
    """Chạy các kiểm tra đang bật.

    diff: None khi không lấy được diff (lỗi tool Git): bỏ hai kiểm tra theo diff, không chặn.
    items: đầu việc của kế hoạch (đã kẹp theo phạm vi của nhãn nếu có).
    item_results: bảng kết quả đầu việc của lần code gần nhất (ItemResult.doc_row).
    intent_text: kế hoạch + nội dung ticket; feedback_text: góp ý của reviewer/tester/người duyệt.
    """
    violations: list[Violation] = []
    owns = declared_owns(items)
    if diff is not None and deletions:
        text = f"{intent_text}\n{feedback_text}"
        bad = tuple(
            p
            for p in diff.deleted
            if p not in system_paths
            and p not in diff.moved
            and not (owns is not None and _allows(owns, p))
            and not mentioned(p, text)
        )
        if bad:
            violations.append(Violation(DELETIONS, bad))
    # owns không xác định thì không có chuẩn để nói file nào "ngoài phạm vi": bỏ kiểm tra này.
    if diff is not None and outside_owns and owns is not None:
        bad = tuple(
            p
            for p in diff.changed
            if p not in system_paths and not _allows(owns, p) and not mentioned(p, feedback_text)
        )
        if bad:
            violations.append(Violation(OUTSIDE_OWNS, bad, owns))
    if unfinished_items:
        rows = [r for r in item_results if not str(r.get("status", "")).startswith("xong")]
        if rows:
            violations.append(
                Violation(
                    UNFINISHED_ITEMS,
                    tuple(str(r["id"]) for r in rows),
                    tuple(f"{r['id']} ({r.get('title', '')}): {r.get('status', '')}" for r in rows),
                )
            )
    return GateResult(tuple(violations))


def _listed(paths: tuple[str, ...]) -> list[str]:
    lines = [f"- {p}" for p in paths[:MAX_LISTED]]
    if len(paths) > MAX_LISTED:
        lines.append(f"- ... và {len(paths) - MAX_LISTED} file khác")
    return lines


def render_feedback(result: GateResult, *, original_ref: str) -> str:
    """Góp ý gửi lại cho bước code: lý do và danh sách file cụ thể.

    original_ref: nhánh giữ nội dung gốc để agent đọc lại file cần khôi phục.
    """
    lines = [
        f"[{GATE_LABEL}] Nhánh chưa được gửi review vì các lỗi quy trình dưới đây. Sửa đúng các "
        "mục này và giữ nguyên những phần khác đã làm.",
    ]
    for n, v in enumerate(result.violations, start=1):
        lines.append("")
        if v.check == DELETIONS:
            lines.append(
                f"{n}. Nhánh xoá file không thuộc phạm vi của đầu việc. Khôi phục đúng nội dung "
                f"gốc của từng file (đọc bằng tool gitlab read_file ở ref `{original_ref}`); "
                "không viết lại theo trí nhớ:"
            )
            lines += _listed(v.paths)
        elif v.check == OUTSIDE_OWNS:
            lines.append(
                f"{n}. Nhánh sửa/thêm file ngoài phạm vi được giao của kế hoạch "
                f"({', '.join(f'`{p}`' for p in v.details)}). Đưa các file sau về nguyên trạng "
                f"(file có sẵn: lấy lại nội dung ở ref `{original_ref}`; file mới thêm: xoá):"
            )
            lines += _listed(v.paths)
        else:
            lines.append(
                f"{n}. Còn đầu việc của kế hoạch chưa xong. Làm cho xong phần việc của các đầu "
                "việc này:"
            )
            lines += [f"- {d}" for d in v.details]
    return "\n".join(lines)


def summary(result: GateResult) -> str:
    """Một dòng cho lý do escalate."""
    names = {
        DELETIONS: "xoá file ngoài phạm vi",
        OUTSIDE_OWNS: "sửa file ngoài owns",
        UNFINISHED_ITEMS: "đầu việc chưa xong",
    }
    return "; ".join(f"{names[v.check]}: {len(v.paths)}" for v in result.violations)
