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

from aico.orchestrator.ports import NO_CHANGE_ERROR
from aico.orchestrator.workplan import ALL_FILES, FIX_ITEM_ID, WorkItem

GATE_LABEL = "cổng trước review"
DELETIONS = "deletions"
OUTSIDE_OWNS = "outside_owns"
UNFINISHED_ITEMS = "unfinished_items"
MAX_LISTED = 40  # số file tối đa liệt kê trong góp ý và event

# File sinh kèm khi làm đúng việc, hiếm khi được kế hoạch hay reviewer nêu đường dẫn: lockfile
# (thêm thư viện), file test và snapshot ("thiếu test hồi quy"). Không tính là sửa ngoài owns.
LOCKFILES = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lockb",
        "bun.lock",
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
        "Cargo.lock",
        "go.sum",
        "composer.lock",
        "Gemfile.lock",
    }
)
TEST_DIRS = frozenset({"test", "tests", "__tests__", "__snapshots__", "spec", "specs", "e2e"})
_TEST_NAME = re.compile(
    r"(^test_.*\.py$)|(_test\.(py|go)$)|(^conftest\.py$)|(\.(test|spec)\.[^.]+$)|(\.snap$)"
)


def incidental(path: str, owned: frozenset[str] = frozenset()) -> bool:
    """File đi kèm hợp lệ dù nằm ngoài owns.

    owned: file trong owns mà nhánh đã đổi. File cùng thư mục, cùng tên gốc với một file như vậy
    (vd `hero.png` sinh từ `hero.pixel.json`) cũng được tính là đi kèm.
    """
    parts = path.split("/")
    name = parts[-1]
    if name in LOCKFILES or _TEST_NAME.search(name) or TEST_DIRS & set(parts[:-1]):
        return True
    folder, stem = "/".join(parts[:-1]), name.split(".", 1)[0]
    return any(
        o.rsplit("/", 1)[-1].split(".", 1)[0] == stem and "/".join(o.split("/")[:-1]) == folder
        for o in owned
    )


@dataclass(frozen=True)
class BranchDiff:
    """Thay đổi của nhánh so với commit chung với nhánh đích."""

    deleted: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()  # thêm mới hoặc đổi nội dung
    # File xoá thực ra là đổi tên/chuyển chỗ: có file thêm mới cùng nội dung hoặc cùng tên.
    moved: frozenset[str] = frozenset()

    @property
    def paths(self) -> tuple[str, ...]:
        return (*self.deleted, *self.changed)


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
    moved = frozenset(
        p for p in deleted if base[p] in added_digests or any(_near_move(p, a) for a in added)
    )
    return BranchDiff(tuple(deleted), tuple(changed), moved)


def _near_move(old: str, new: str) -> bool:
    """Cùng tên file và chỉ đổi thư mục ở gần (mỗi bên lệch tối đa một cấp so với phần chung).

    Chỉ cùng tên thôi thì không đủ: xoá `src/core/index.ts` rồi thêm `docs/index.ts` không phải
    là chuyển chỗ.
    """
    a, b = old.split("/"), new.split("/")
    if a[-1] != b[-1]:
        return False
    da, db = a[:-1], b[:-1]
    common = 0
    while common < min(len(da), len(db)) and da[common] == db[common]:
        common += 1
    return len(da) - common <= 1 and len(db) - common <= 1


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
    accepted: frozenset[str] = frozenset(),
    retried: frozenset[str] = frozenset(),
    deletions: bool = True,
    outside_owns: bool = True,
    unfinished_items: bool = True,
) -> GateResult:
    """Chạy các kiểm tra đang bật.

    diff: None khi không lấy được diff (lỗi tool Git): bỏ hai kiểm tra theo diff, không chặn.
    items: đầu việc của kế hoạch (đã kẹp theo phạm vi của nhãn nếu có).
    item_results: bảng kết quả đầu việc của lần code gần nhất (ItemResult.doc_row).
    intent_text: kế hoạch + nội dung ticket; feedback_text: mọi góp ý của reviewer/tester/người
    duyệt ticket từng nhận (diff cộng dồn cả nhánh nên góp ý cũng cộng dồn).
    accepted: file đã có trên nhánh ở lần gần nhất nhánh được gửi review; chỉ xét phần mới.
    retried: đầu việc đã được chạy lại; vẫn "không sửa file nào" thì để reviewer xét, không chặn.
    """
    violations: list[Violation] = []
    notes: list[str] = []
    owns = declared_owns(items)
    if diff is not None and deletions:
        text = f"{intent_text}\n{feedback_text}"
        bad = tuple(
            p
            for p in diff.deleted
            if p not in system_paths
            and p not in accepted
            and p not in diff.moved
            and not (owns is not None and _allows(owns, p))
            and not mentioned(p, text)
        )
        if bad:
            violations.append(Violation(DELETIONS, bad))
    # owns không xác định thì không có chuẩn để nói file nào "ngoài phạm vi": bỏ kiểm tra này.
    if diff is not None and outside_owns and owns is not None:
        owned = frozenset(p for p in diff.changed if _allows(owns, p))
        bad = tuple(
            p
            for p in diff.changed
            if p not in system_paths
            and p not in accepted
            and p not in owned
            and not incidental(p, owned)
            and not mentioned(p, feedback_text)
        )
        if bad:
            violations.append(Violation(OUTSIDE_OWNS, bad, owns))
    if unfinished_items:
        rows = []
        for r in item_results:
            status = str(r.get("status", ""))
            if status.startswith("xong") or r.get("id") == FIX_ITEM_ID:
                continue
            if r.get("id") in retried and NO_CHANGE_ERROR in status:
                # Chạy lại vẫn không sửa gì: agent thấy không cần sửa hoặc owns của kế hoạch sai.
                # Không phải việc cổng phán: để reviewer xét.
                notes.append(f"đầu việc {r['id']} chạy lại vẫn không sửa file nào: để reviewer xét")
                continue
            rows.append(r)
        if rows:
            violations.append(
                Violation(
                    UNFINISHED_ITEMS,
                    tuple(str(r["id"]) for r in rows),
                    tuple(f"{r['id']} ({r.get('title', '')}): {r.get('status', '')}" for r in rows),
                )
            )
    return GateResult(tuple(violations), tuple(notes))


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
