"""Hoạt động chi tiết của agent đọc từ transcript của CLI (deploy/worker/activity.py)."""

from __future__ import annotations

import importlib.util
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "task-token-0123456789abcdef"


def _load(name: str, rel: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


act = _load("activity", "deploy/worker/activity.py")
ws = _load("worker_server_activity", "deploy/worker/worker_server.py")

CWD = "/tmp/job-_uffp4vl/repo"

# Dòng mẫu theo đúng dạng transcript thật của từng CLI (nội dung đã thay).
CLAUDE_LINES = [
    {"type": "queue-operation", "operation": "enqueue"},
    {
        "type": "assistant",
        "timestamp": "2026-10-21T08:00:00Z",
        "message": {
            "content": [
                {"type": "thinking", "thinking": "..."},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Read",
                    "input": {"file_path": f"{CWD}/src/scene/actors.ts"},
                },
            ],
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5},
        },
    },
    {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "npm test"}}
            ]
        },
    },
    {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "is_error": True}]},
    },
    {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "TodoWrite",
                    "input": {
                        "todos": [
                            {"content": "Sửa layout", "status": "completed"},
                            {"content": "Thêm test", "status": "in_progress"},
                        ]
                    },
                },
                {"type": "tool_use", "name": "mcp__aico__push", "input": {"files": []}},
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"command": "aico-remote 'npm run build'"},
                },
            ]
        },
    },
]

CURSOR_LINES = [
    {"role": "user", "message": {"content": [{"type": "text", "text": "prompt"}]}},
    {
        "role": "assistant",
        "message": {"content": [{"type": "text", "text": "Mình sẽ đọc file trước."}]},
    },
    {
        "role": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Grep", "input": {"pattern": "seated", "path": "src"}}
            ]
        },
    },
    {
        "role": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "StrReplace",
                    "input": {"path": f"{CWD}/src/main.ts", "old_string": "a", "new_string": "b"},
                }
            ]
        },
    },
    {
        "role": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Shell",
                    "input": {
                        "command": f"git diff --stat && echo Bearer {TOKEN}",
                        "description": "x",
                    },
                }
            ]
        },
    },
    {
        "role": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "CallDynamicTool",
                    "input": {
                        "namespace": "aico",
                        "toolName": "read_file",
                        "arguments": {"path": "README.md"},
                    },
                }
            ]
        },
    },
    {"type": "turn_ended", "status": "success"},
]

CODEX_LINES = [
    {"type": "session_meta", "payload": {"cwd": CWD}},
    {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {
                "type": "AgentMessage",
                "phase": "commentary",
                "content": [{"type": "Text", "text": "I'll run ls."}],
            },
        },
    },
    {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {
                "type": "CommandExecution",
                "command": ["/bin/bash", "-lc", "cat note.txt"],
                "parsed_cmd": [{"type": "read", "path": "note.txt"}],
                "exit_code": 0,
            },
        },
    },
    {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {
                "type": "CommandExecution",
                "command": ["/bin/bash", "-lc", "pytest -q"],
                "parsed_cmd": [{"type": "unknown"}],
                "exit_code": 1,
            },
        },
    },
    {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": {"input_tokens": 1200, "output_tokens": 80}},
        },
    },
    {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {
                "type": "AgentMessage",
                "phase": "final_answer",
                "content": [{"type": "Text", "text": "DONE"}],
            },
        },
    },
]


def _activities(kind: str, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for line in lines:
        out += [v for name, v in act.parse_line(kind, line, CWD) if name == "activity"]
    return out


def test_claude_transcript_activities() -> None:
    acts = _activities("claude", CLAUDE_LINES)
    assert [a["kind"] for a in acts] == ["think", "read", "test", "plan", "git", "remote"]
    assert acts[1]["text"] == "Đọc scene/actors.ts"
    assert acts[4]["text"] == "Đẩy code lên nhánh"
    assert acts[5]["text"] == "Build/test trên server"
    state = act.ActivityState()
    for line in CLAUDE_LINES:
        state.apply(act.parse_line("claude", line, CWD))
    snap = state.snapshot()
    assert snap["tasks_done"] == 1 and [t["status"] for t in snap["tasks"]] == [
        "completed",
        "in_progress",
    ]
    assert snap["stats"] == {"tool_calls": 5, "tokens_in": 100, "tokens_out": 5}
    test_run = next(a for a in snap["recent"] if a["kind"] == "test")
    assert test_run.get("error") is True  # tool_result lỗi đánh dấu lời gọi vừa rồi


def test_cursor_transcript_activities_and_masking() -> None:
    acts = _activities("cursor", CURSOR_LINES)
    assert [a["kind"] for a in acts] == ["respond", "search", "edit", "git", "read"]
    assert acts[2]["text"] == "Sửa src/main.ts"
    assert acts[4]["text"] == "Đọc README.md"
    state = act.ActivityState([TOKEN])
    for line in CURSOR_LINES:
        state.apply(act.parse_line("cursor", line, CWD))
    assert TOKEN not in json.dumps(state.snapshot())


def test_codex_rollout_activities() -> None:
    acts = _activities("codex", CODEX_LINES)
    assert [a["kind"] for a in acts] == ["respond", "read", "test"]
    assert acts[1]["text"] == "Đọc note.txt"
    assert acts[2].get("error") is True
    state = act.ActivityState()
    for line in CODEX_LINES:
        state.apply(act.parse_line("codex", line, CWD))
    assert state.snapshot()["stats"]["tokens_in"] == 1200


def test_mask_patterns() -> None:
    text = (
        "Authorization: Bearer abcdefghijkl GITHUB_TOKEN=ghp_aaaaaaaaaaaaaaaaaaaa "
        "sk-proj-123456789 https://user:pa55word@host/x eyJhbGciOiJI.eyJzdWIiOiIx.c2lnbmF0dXJl"
    )
    masked = act.mask(text)
    for leak in ("abcdefghijkl", "ghp_aaaa", "sk-proj", "pa55word", "eyJhbGci"):
        assert leak not in masked
    assert "Bearer ***" in masked and "GITHUB_TOKEN=***" in masked and "user:***@host" in masked


def _write(path: Path, lines: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")


def test_find_transcript_per_cli(tmp_path: Path) -> None:
    since = time.time()
    claude = tmp_path / ".claude/projects/-tmp-job--uffp4vl-repo/s1.jsonl"
    cursor = tmp_path / ".cursor/projects/tmp-job--uffp4vl-repo/agent-transcripts/a/a.jsonl"
    codex_other = tmp_path / ".codex/sessions/2026/10/21/rollout-a.jsonl"
    codex_mine = tmp_path / ".codex/sessions/2026/10/21/rollout-b.jsonl"
    _write(claude, CLAUDE_LINES[:1])
    _write(cursor, CURSOR_LINES[:1])
    _write(codex_other, [{"type": "session_meta", "payload": {"cwd": "/tmp/job-other"}}])
    _write(codex_mine, CODEX_LINES[:1])
    assert act.find_transcript("claude", tmp_path, CWD, since) == claude
    assert act.find_transcript("cursor", tmp_path, CWD, since) == cursor
    assert act.find_transcript("codex", tmp_path, CWD, since) == codex_mine
    old = since - 3600
    os.utime(claude, (old, old))  # transcript của lần chạy trước: không nhận
    assert act.find_transcript("claude", tmp_path, CWD, since) is None


def test_tracker_reads_incrementally_and_partial_lines(tmp_path: Path) -> None:
    path = tmp_path / ".claude/projects/-tmp-job--uffp4vl-repo/s.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")
    updates: list[dict[str, Any]] = []
    hb = tmp_path / "hb"
    tracker = act.ActivityTracker("claude", tmp_path, Path(CWD), updates.append, heartbeat=hb)
    tracker.poll()
    assert updates == [] and tracker.state.source == "transcript"
    line = json.dumps(CLAUDE_LINES[1]) + "\n" + json.dumps(CLAUDE_LINES[2])
    with path.open("a", encoding="utf-8") as f:
        f.write(line[:-5])  # dòng thứ hai chưa ghi xong
    tracker.poll()
    assert updates[-1]["activity"]["kind"] == "read" and hb.exists()
    with path.open("a", encoding="utf-8") as f:
        f.write(line[-5:] + "\n")
    tracker.poll()
    assert updates[-1]["activity"]["kind"] == "test"
    assert len(updates[-1]["recent"]) == 3


def test_worker_stats_snapshot_includes_activity() -> None:
    stats = ws.WorkerStats(Path(os.devnull))
    now = datetime.now(UTC)
    agent_id = stats.start({"role": "coder", "ticket_id": "T-1", "mode": "write"}, now)
    stats.update(agent_id, {"activity": {"kind": "edit", "text": "Sửa a.ts"}, "tasks": []})
    agent = stats.snapshot("claude", 2, now)["agents"][0]
    assert agent["ticket_id"] == "T-1" and agent["activity"]["text"] == "Sửa a.ts"
    stats.update("khong-co", {"activity": {}})  # agent đã xong: bỏ qua
    stats.finish(agent_id, {"ok": True}, (1, 1), now)
    stats.update(agent_id, {"activity": {}})
    assert stats.snapshot("claude", 2, now)["agents"] == []


def test_codex_keeps_rollout_and_prunes_old(tmp_path: Path) -> None:
    job = {"prompt": "p", "mcp_url": "http://x", "mcp_token": TOKEN, "mode": "read"}
    cmd, _ = ws.build_command(job, "codex", tmp_path)
    assert "--ephemeral" not in cmd
    old = tmp_path / ".codex/sessions/2026/10/01/rollout-old.jsonl"
    new = tmp_path / ".codex/sessions/2026/10/21/rollout-new.jsonl"
    _write(old, CODEX_LINES[:1])
    _write(new, CODEX_LINES[:1])
    week_ago = time.time() - 8 * 86400
    os.utime(old, (week_ago, week_ago))
    ws.prune_codex_sessions(tmp_path)
    assert not old.exists() and new.exists()
