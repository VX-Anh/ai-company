"""Mốc cấu hình cho canary: mốc deploy của aico-ops.sh, dấu vân tay company/, cỡ diff của MR."""

from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from aico.agents.cli_agent import company_sha, diff_size
from aico.registry import company_fingerprint
from aico.rnd.canary import sha_matches

OPS = Path(__file__).resolve().parents[1] / "deploy" / "aico-ops.sh"


def _bash() -> str | None:
    """bash chạy được script. Windows: lấy bash của Git (bash.exe trong System32 là WSL)."""
    if sys.platform != "win32":
        return shutil.which("bash")
    git = shutil.which("git")
    if git is None:
        return None
    for parent in list(Path(git).resolve().parents)[:3]:
        for candidate in (parent / "bin" / "bash.exe", parent / "usr" / "bin" / "bash.exe"):
            if candidate.is_file():
                return str(candidate)
    return None


BASH = _bash()
needs_bash = pytest.mark.skipif(
    BASH is None or shutil.which("git") is None, reason="cần bash và git"
)


def _ops(tmp_path: Path, *call: str) -> str:
    """Gọi một hàm của aico-ops.sh (source, không chạy lệnh) với SRC/STACK trỏ vào thư mục tạm."""
    assert BASH is not None
    shim = tmp_path / "shim"
    if not shim.exists():  # máy Windows không có lệnh python3
        shim.mkdir()
        (shim / "python3").write_text(
            f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "$@"\n',
            encoding="utf-8",
            newline="\n",
        )
        (shim / "python3").chmod(0o755)
    script = (
        'bin="$1"; ops="$2"; shift 2; '
        'command -v cygpath >/dev/null && bin="$(cygpath -u "$bin")"; '
        'PATH="$bin:$PATH"; source "$ops"; "$@"'
    )
    env = {
        **os.environ,
        "AICO_SRC": (tmp_path / "src").as_posix(),
        "AICO_STACK": (tmp_path / "stack").as_posix(),
    }
    done = subprocess.run(
        [BASH, "-lc", script, "ops", shim.as_posix(), OPS.as_posix(), *call],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@needs_bash
def test_deploy_mark_tells_dirty_deploys_apart_and_sees_untracked_files(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "stack").mkdir()
    _git(src, "init", "-q")
    (src / "a.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    _git(src, "add", "a.py")
    _git(src, "commit", "-q", "-m", "init")
    head = _git(src, "rev-parse", "HEAD")

    clean = _ops(tmp_path, "deploy_mark")
    assert clean == head[:11]
    # Hai lần deploy bẩn khác nhau trên cùng commit ra hai mốc khác nhau (trước đây cùng "<sha>d").
    (src / "a.py").write_text("x = 2\n", encoding="utf-8", newline="\n")
    dirty1 = _ops(tmp_path, "deploy_mark")
    assert re.fullmatch(rf"{clean}x[0-9a-f]{{8}}", dirty1)
    assert _ops(tmp_path, "deploy_mark") == dirty1  # cùng nội dung thì cùng mốc
    (src / "a.py").write_text("x = 3\n", encoding="utf-8", newline="\n")
    dirty2 = _ops(tmp_path, "deploy_mark")
    assert re.fullmatch(rf"{clean}x[0-9a-f]{{8}}", dirty2) and dirty2 != dirty1
    # File chưa theo dõi cũng làm bản deploy bẩn, và sửa tiếp nội dung của nó đổi mốc.
    (src / "a.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    assert _ops(tmp_path, "deploy_mark") == clean
    (src / "new_prompt.md").write_text("v1\n", encoding="utf-8", newline="\n")
    untracked1 = _ops(tmp_path, "deploy_mark")
    (src / "new_prompt.md").write_text("v2\n", encoding="utf-8", newline="\n")
    untracked2 = _ops(tmp_path, "deploy_mark")
    assert untracked1.startswith(f"{clean}x") and untracked2.startswith(f"{clean}x")
    assert len({dirty1, dirty2, untracked1, untracked2}) == 4
    # Mốc đi trọn qua company_sha (không bị cắt) và khớp đúng quy tắc của canary.
    recorded = company_sha("abc123abc123", dirty1)
    assert recorded == f"{dirty1}-abc123abc123"
    assert sha_matches(recorded, dirty1) and not sha_matches(recorded, head)
    assert sha_matches(company_sha("abc123abc123", clean), head)
    # Không phải repo git: mốc rỗng, không lỗi.
    shutil.rmtree(src / ".git", ignore_errors=True)
    if not (src / ".git").exists():
        assert _ops(tmp_path, "deploy_mark") == ""


@needs_bash
def test_write_mark_updates_env_file_without_printing_it(tmp_path: Path) -> None:
    stack = tmp_path / "stack"
    stack.mkdir()
    (tmp_path / "src").mkdir()
    env = stack / ".env"
    # Chưa có .env: tạo mới.
    assert _ops(tmp_path, "write_mark", "aaaaaaaaaaa") == ""
    assert env.read_text(encoding="utf-8") == "AICO_COMPANY_SHA=aaaaaaaaaaa\n"
    # Có sẵn: giữ nguyên các dòng khác (kể cả dòng cuối thiếu xuống dòng), thay đúng một dòng mốc,
    # không in nội dung .env ra.
    env.write_text(
        "POSTGRES_PASSWORD=s3cret\nAICO_COMPANY_SHA=cu\n# AICO_COMPANY_SHA=ghi chu\nX=1",
        encoding="utf-8",
        newline="\n",
    )
    out = _ops(tmp_path, "write_mark", "bbbbbbbbbbbx1a2b3c4d")
    assert "s3cret" not in out
    assert env.read_text(encoding="utf-8").splitlines() == [
        "POSTGRES_PASSWORD=s3cret",
        "# AICO_COMPANY_SHA=ghi chu",
        "X=1",
        "AICO_COMPANY_SHA=bbbbbbbbbbbx1a2b3c4d",
    ]
    # Mốc rỗng (không đọc được git) vẫn ghi đè để mốc cũ không sống tiếp.
    _ops(tmp_path, "write_mark", "")
    assert env.read_text(encoding="utf-8").splitlines()[-1] == "AICO_COMPANY_SHA="
    assert [p.name for p in stack.iterdir()] == [".env"]  # không để lại file tạm


@needs_bash
def test_canary_query_accepts_dash_and_url_encodes(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "stack").mkdir()
    assert _ops(tmp_path, "canary_query") == "n=10"
    assert _ops(tmp_path, "canary_query", "-", "5") == "n=5"  # "-" = cấu hình mới nhất
    assert _ops(tmp_path, "canary_query", "8f9b4f9c0ffx1a2b3c4d") == "n=10&sha=8f9b4f9c0ffx1a2b3c4d"
    # Ký tự đặc biệt không chen được tham số khác vào URL.
    assert _ops(tmp_path, "canary_query", "a b&days=1#", "3") == "n=3&sha=a+b%26days%3D1%23"


def test_fingerprint_ignores_hidden_and_non_config_files(company_copy: Path) -> None:
    base = company_fingerprint(company_copy)
    prompts = company_copy / "prompts"
    # File tạm của trình soạn thảo, file ẩn, thư mục ẩn, đuôi khác: không mở đợt canary mới.
    (prompts / ".coder.md.swp").write_bytes(b"\x00tam")
    (prompts / ".hidden.md").write_text("an\n", encoding="utf-8")
    (prompts / "coder.md~").write_text("ban sao\n", encoding="utf-8")
    (prompts / "coder.md.orig").write_text("ban goc\n", encoding="utf-8")
    (company_copy / "roles" / ".DS_Store").write_bytes(b"\x00\x01")
    (company_copy / "roles" / ".cache").mkdir()
    (company_copy / "roles" / ".cache" / "x.yaml").write_text("a: 1\n", encoding="utf-8")
    assert company_fingerprint(company_copy) == base
    # File cấu hình thật (*.md, *.yaml, *.yml) thì vẫn tính, kể cả trong thư mục con.
    (prompts / "extra.md").write_text("them\n", encoding="utf-8")
    with_md = company_fingerprint(company_copy)
    assert with_md != base
    (company_copy / "policies" / "sub").mkdir()
    (company_copy / "policies" / "sub" / "more.YML").write_text("a: 1\n", encoding="utf-8")
    assert company_fingerprint(company_copy) not in (base, with_md)


def test_diff_size_counts_content_lines_that_look_like_headers() -> None:
    # Dòng SQL "-- chú thích" bị xoá hiện trong diff là "--- chú thích", dòng "++ x" được thêm là
    # "+++ x": lọc header theo nội dung từng làm mất các dòng này.
    old = ["-- xoa bang cu\n", "DROP TABLE a;\n", "giu\n"]
    new = ["giu\n", "++ them\n", "-- chu thich moi\n"]
    chunk = list(difflib.unified_diff(old, new, fromfile="a/m.sql", tofile="b/m.sql"))
    assert chunk[0].startswith("--- a/") and chunk[1].startswith("+++ b/")
    assert "--- xoa bang cu\n" in chunk and "+++ them\n" in chunk
    assert diff_size([chunk]) == {"diff_files": 1, "diff_lines": 4}
    # File rỗng mới thêm: diff không có dòng nào, vẫn là một file đổi.
    assert diff_size([[]]) == {"diff_files": 1, "diff_lines": 0}
