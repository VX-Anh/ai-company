"""Rào chắn cho PR tự cải tiến của Phòng R&D (R2): hệ không được tự nới quyền cho chính mình.

PR từ nhánh `rnd/*` vào ai-company chỉ được:
- sửa prompt vai trò đã có: `company/prompts/*.md`;
- sửa `workers` (thứ tự phòng) và `limits` (vòng lặp, thời gian) của vai trò đã có trong
  `company/roles/*.yaml`.
Mọi thứ khác (policy, tool, max_risk, constraints, code, thêm/xoá vai trò, SOP) bị chặn ở CI,
không trông vào việc agent làm đúng prompt. Owner vẫn duyệt PR và deploy tay.
"""

from __future__ import annotations

import fnmatch
import subprocess
from collections.abc import Mapping
from pathlib import Path

import yaml

SELFIMPROVE_BRANCH = "rnd/*"
PROMPTS = "company/prompts/*.md"
ROLES = "company/roles/*.yaml"
# Khoá của vai trò mà thí nghiệm được đổi; mọi khoá khác phải giữ nguyên.
TUNABLE_ROLE_KEYS = frozenset({"workers", "limits"})

# đường dẫn -> (nội dung ở base hoặc None nếu file mới, nội dung ở head hoặc None nếu bị xoá)
Changes = Mapping[str, tuple[str | None, str | None]]


def _role_without_tunables(text: str) -> dict[str, object] | None:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        return None
    return {k: v for k, v in data.items() if k not in TUNABLE_ROLE_KEYS}


def violations(changes: Changes) -> list[str]:
    """Lý do PR tự cải tiến bị chặn; rỗng là hợp lệ."""
    out: list[str] = []
    for path, (old, new) in sorted(changes.items()):
        if old is None or new is None:
            out.append(f"{path}: không được thêm hoặc xoá file (chỉ sửa prompt/vai trò đã có)")
            continue
        if fnmatch.fnmatch(path, PROMPTS):
            continue
        if fnmatch.fnmatch(path, ROLES):
            try:
                before, after = _role_without_tunables(old), _role_without_tunables(new)
            except yaml.YAMLError as exc:
                out.append(f"{path}: YAML không hợp lệ: {exc}")
                continue
            if before is None or after is None:
                out.append(f"{path}: vai trò phải là mapping YAML")
            elif before != after:
                changed = sorted(
                    k for k in set(before) | set(after) if before.get(k) != after.get(k)
                )
                out.append(
                    f"{path}: chỉ được đổi {', '.join(sorted(TUNABLE_ROLE_KEYS))}; "
                    f"đã đổi {', '.join(changed)}"
                )
            continue
        out.append(f"{path}: ngoài phạm vi tự cải tiến ({PROMPTS}, {ROLES})")
    return out


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True, encoding="utf-8"
    ).stdout


def git_changes(root: Path, base: str) -> dict[str, tuple[str | None, str | None]]:
    """Thay đổi của HEAD so với base (điểm rẽ nhánh), đọc nội dung cả hai phía."""
    status = _git(root, "diff", "--name-status", "--no-renames", f"{base}...HEAD")
    merge_base = _git(root, "merge-base", base, "HEAD").strip()
    changes: dict[str, tuple[str | None, str | None]] = {}
    for line in status.splitlines():
        code, _, path = line.partition("\t")
        old = None if code.startswith("A") else _git(root, "show", f"{merge_base}:{path}")
        new = None if code.startswith("D") else _git(root, "show", f"HEAD:{path}")
        changes[path] = (old, new)
    return changes


def check(root: Path, base: str) -> list[str]:
    return violations(git_changes(root, base))
