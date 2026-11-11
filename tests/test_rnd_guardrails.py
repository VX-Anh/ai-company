"""Rào chắn PR tự cải tiến: hệ không tự nới quyền cho chính mình (R2)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from aico.rnd.guardrails import check, violations

ROLE = """id: coder
worker: cli
workers: [claude, codex, cursor]
tools:
  - server: gitlab
    allow: [read_file, push]
max_risk: L2
limits:
  timeout_minutes: 30
"""


def test_prompt_and_tunable_role_keys_are_allowed() -> None:
    tuned = ROLE.replace("[claude, codex, cursor]", "[codex, claude]").replace("30", "45")
    assert (
        violations(
            {
                "company/prompts/coder.md": ("cũ", "mới"),
                "company/roles/coder.yaml": (ROLE, tuned),
            }
        )
        == []
    )


def test_permissions_policy_code_and_new_files_are_blocked() -> None:
    wider = ROLE.replace("max_risk: L2", "max_risk: L3").replace("push]", "push, merge_mr]")
    problems = violations(
        {
            "company/roles/coder.yaml": (ROLE, wider),
            "company/policies/default.yaml": ("a: 1", "a: 2"),
            "src/aico/tools/guard.py": ("x", "y"),
            "company/prompts/new_role.md": (None, "mới"),
            "company/roles/merge_bot.yaml": (ROLE, None),
        }
    )
    assert len(problems) == 5
    coder = next(p for p in problems if p.startswith("company/roles/coder.yaml"))
    assert "max_risk" in coder and "tools" in coder
    assert any("ngoài phạm vi" in p and "policies" in p for p in problems)


def test_check_reads_git_diff_against_base(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "company" / "roles").mkdir(parents=True)
    (tmp_path / "company" / "prompts").mkdir(parents=True)
    (tmp_path / "company" / "roles" / "coder.yaml").write_text(ROLE, encoding="utf-8")
    (tmp_path / "company" / "prompts" / "coder.md").write_text("cũ\n", encoding="utf-8")
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-qm", "base")
    git("switch", "-qc", "rnd/thu-prompt")
    (tmp_path / "company" / "prompts" / "coder.md").write_text("mới\n", encoding="utf-8")
    git("commit", "-qam", "prompt")
    assert check(tmp_path, "main") == []
    role = tmp_path / "company" / "roles" / "coder.yaml"
    role.write_text(ROLE.replace("L2", "L3"), encoding="utf-8")
    git("commit", "-qam", "nới quyền")
    assert len(check(tmp_path, "main")) == 1


def test_symlinked_prompt_and_huge_limits_are_blocked(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "company" / "roles").mkdir(parents=True)
    (tmp_path / "company" / "prompts").mkdir(parents=True)
    (tmp_path / "company" / "roles" / "coder.yaml").write_text(ROLE, encoding="utf-8")
    prompt = tmp_path / "company" / "prompts" / "coder.md"
    prompt.write_text("cũ\n", encoding="utf-8")
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    git("config", "core.symlinks", "true")
    git("add", "-A")
    git("commit", "-qm", "base")
    git("switch", "-qc", "rnd/x")
    role = tmp_path / "company" / "roles" / "coder.yaml"
    role.write_text(
        ROLE.replace("timeout_minutes: 30", "timeout_minutes: 100000"), encoding="utf-8"
    )
    git("commit", "-qam", "limits")
    problems = check(tmp_path, "main", "rnd/x")
    assert any("vượt trần" in p for p in problems), problems
    # Symlink tạo bằng plumbing của Git (Windows không cần quyền tạo symlink thật).
    blob = (
        subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=tmp_path,
            input=b"/run/secrets/gitlab_token",
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )
    git("update-index", "--cacheinfo", f"120000,{blob},company/prompts/coder.md")
    git("commit", "-qm", "symlink")
    assert any("file thường" in p for p in check(tmp_path, "main", "rnd/x"))
