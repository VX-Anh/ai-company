"""Hoạt động chi tiết của agent, đọc từ transcript mà chính CLI ghi (chỉ thư viện chuẩn).

Mỗi CLI ghi transcript JSONL trong home của worker trong lúc chạy:
- Claude Code: ~/.claude/projects/<cwd, ký tự lạ thành '-'>/<session>.jsonl
- Cursor: ~/.cursor/projects/<như trên, bỏ '-' đầu>/agent-transcripts/<id>/<id>.jsonl
- Codex: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl (dòng đầu session_meta có cwd)

parse_line() là hàm thuần: một dòng -> danh sách sự kiện. ActivityTracker đọc phần mới của
transcript theo chu kỳ và giữ trạng thái cho GET /status. Chỉ giữ tóm tắt đã che secret, không
giữ hội thoại.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

TEXT_MAX = 46
DETAIL_MAX = 300
RECENT_MAX = 20

ICONS = {
    "think": "💭",
    "respond": "💬",
    "read": "📖",
    "search": "🔍",
    "edit": "✏️",
    "write": "📝",
    "run": "💻",
    "test": "🧪",
    "git": "🌿",
    "web": "🌐",
    "plan": "📋",
    "delegate": "👥",
    "mcp": "🔌",
    "remote": "🚀",
    "error": "⚠️",
    "other": "⚙️",
}

# Tool của aico (MCP) -> loại hoạt động
AICO_TOOLS = {
    "read_file": "read",
    "list_issues": "read",
    "get_merge_request": "read",
    "download_source": "read",
    "create_branch": "git",
    "push": "git",
    "open_merge_request": "git",
    "merge_mr": "git",
}

_SECRET_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1***"),
    (
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY)[A-Z0-9_]*\s*[=:]\s*)[^\s'\"]+"
        ),
        r"\1***",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "***"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{12,}|github_pat_[A-Za-z0-9_]{12,})"), "***"),
    (re.compile(r"\bAKIA[A-Z0-9]{12,}"), "***"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "***"),
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+@"), r"\1***@"),
]
_TEST = re.compile(
    r"\b(pytest|vitest|jest|npm (run )?test|pnpm test|yarn test|go test|cargo test)\b"
)
_GIT = re.compile(r"^\s*(git|gh)\s")
_REMOTE = re.compile(r"\baico-remote\b")


def mask(text: str, secrets: Iterable[str] = ()) -> str:
    """Che secret biết trước (thay chính xác) và secret theo mẫu (Bearer, TOKEN=, ghp_...)."""
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, "***")
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _cut(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def short_path(path: str, cwd: str = "") -> str:
    """Đường dẫn gọn: bỏ thư mục job, giữ tối đa 2 phần cuối."""
    p = path.strip()
    if cwd and p.startswith(cwd.rstrip("/") + "/"):
        p = p[len(cwd.rstrip("/")) + 1 :]
    parts = PurePosixPath(p).parts
    return "/".join(parts[-2:]) if len(parts) > 2 else p


def classify_command(cmd: str) -> str:
    if _REMOTE.search(cmd):
        return "remote"
    if _TEST.search(cmd):
        return "test"
    if _GIT.search(cmd):
        return "git"
    return "run"


def make(kind: str, text: str, *, tool: str = "", detail: str = "", at: str = "") -> dict[str, Any]:
    act: dict[str, Any] = {
        "kind": kind,
        "icon": ICONS.get(kind, ICONS["other"]),
        "text": _cut(text, TEXT_MAX),
        "at": at or datetime.now(UTC).isoformat(),
    }
    if tool:
        act["tool"] = tool
    if detail:
        act["detail"] = _cut(detail, DETAIL_MAX)
    return act


def _command_activity(cmd: str, tool: str, at: str) -> dict[str, Any]:
    kind = classify_command(cmd)
    text = {
        "remote": "Build/test trên server",
        "test": "Chạy test",
        "git": f"Git: {cmd.strip()}",
    }.get(kind, f"Chạy {cmd.strip()}")
    return make(kind, text, tool=tool, detail=cmd, at=at)


def tool_activity(name: str, args: Any, cwd: str = "", at: str = "") -> dict[str, Any]:
    """Một lời gọi tool (tên theo Claude/Cursor) -> hoạt động."""
    a: dict[str, Any] = args if isinstance(args, dict) else {}
    path = str(a.get("file_path") or a.get("path") or a.get("notebook_path") or "")
    if name.startswith("mcp__"):
        tool = name.rsplit("__", 1)[-1]
        return _aico_activity(tool, a, at)
    if name == "CallDynamicTool":  # Cursor gọi tool MCP qua tool trung gian
        inner = a.get("arguments")
        inner_args = inner if isinstance(inner, dict) else {}
        return _aico_activity(str(a.get("toolName") or ""), inner_args, at)
    if name in ("Read", "NotebookRead", "view"):
        return make("read", f"Đọc {short_path(path, cwd)}", tool=name, at=at)
    if name in ("Glob", "Grep", "LS", "list_dir", "codebase_search"):
        pattern = str(a.get("pattern") or a.get("glob_pattern") or a.get("query") or path)
        return make("search", f"Tìm {pattern}", tool=name, at=at)
    if name in ("Edit", "MultiEdit", "StrReplace", "NotebookEdit", "apply_patch"):
        text = f"Sửa {short_path(path, cwd)}" if path else "Sửa code"
        return make("edit", text, tool=name, at=at)
    if name == "Write":
        return make("write", f"Tạo {short_path(path, cwd)}", tool=name, at=at)
    if name in ("Bash", "Shell", "shell", "exec_command"):
        cmd = a.get("command") or a.get("cmd") or ""
        cmd = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        return _command_activity(cmd, name, at)
    if name in ("WebFetch", "WebSearch"):
        return make("web", f"Xem web {a.get('url') or a.get('query') or ''}", tool=name, at=at)
    if name in ("TodoWrite", "update_plan", "TaskCreate", "TaskUpdate"):
        return make("plan", "Cập nhật danh sách việc", tool=name, at=at)
    if name in ("Task", "Agent"):
        desc = str(a.get("description") or "")
        text = f"Giao việc: {desc}" if desc else "Giao việc cho agent con"
        return make("delegate", text, tool=name, at=at)
    return make("other", name, tool=name, at=at)


def _aico_activity(tool: str, args: dict[str, Any], at: str) -> dict[str, Any]:
    if tool.startswith("pixel_"):
        return make("mcp", f"Vẽ pixel: {tool[6:]}", tool=tool, at=at)
    kind = AICO_TOOLS.get(tool, "mcp")
    path = str(args.get("path") or "")
    if tool == "read_file" and path:
        return make("read", f"Đọc {short_path(path)}", tool=tool, at=at)
    labels = {
        "push": "Đẩy code lên nhánh",
        "open_merge_request": "Mở merge request",
        "create_branch": "Tạo nhánh",
        "merge_mr": "Merge",
        "list_issues": "Xem issue",
        "get_merge_request": "Xem merge request",
        "download_source": "Tải mã nguồn",
    }
    return make(kind, labels.get(tool, f"aico: {tool}"), tool=tool, at=at)


def _todos(args: Any) -> list[dict[str, str]] | None:
    """Danh sách task từ TodoWrite/update_plan."""
    if not isinstance(args, dict):
        return None
    items = args.get("todos") or args.get("plan")
    if not isinstance(items, list):
        return None
    out = []
    for i, t in enumerate(items):
        if isinstance(t, dict):
            title = str(t.get("content") or t.get("step") or t.get("title") or "")
            status = str(t.get("status") or "pending")
            out.append({"id": str(t.get("id") or i), "title": _cut(title, 80), "status": status})
    return out


# ("activity", dict) | ("tasks", list) | ("tokens"/"tokens_total", (in, out)) | ("error", id)
Event = tuple[str, Any]


def parse_line(kind: str, obj: Any, cwd: str = "") -> list[Event]:
    """Một dòng transcript (đã json.loads) -> các sự kiện. Dòng lạ thì trả rỗng."""
    if not isinstance(obj, dict):
        return []
    if kind == "codex":
        return _parse_codex(obj, cwd)
    return _parse_claude_like(obj, cwd)


def _parse_claude_like(obj: dict[str, Any], cwd: str) -> list[Event]:
    """Claude Code (type assistant/user) và Cursor (role assistant): cùng dạng message.content."""
    role = obj.get("type") or obj.get("role")
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return []
    at = str(obj.get("timestamp") or "")
    content = msg.get("content")
    events: list[Event] = []
    if role == "assistant":
        usage = msg.get("usage")
        if isinstance(usage, dict):
            inp = sum(
                int(usage.get(k) or 0)
                for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
            )
            events.append(("tokens", (inp, int(usage.get("output_tokens") or 0))))
        for c in content if isinstance(content, list) else []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use":
                name, args = str(c.get("name") or ""), c.get("input")
                events.append(("activity", tool_activity(name, args, cwd, at)))
                if (todos := _todos(args)) is not None and name in ("TodoWrite", "update_plan"):
                    events.append(("tasks", todos))
            elif c.get("type") == "thinking":
                events.append(("activity", make("think", "Đang suy nghĩ", at=at)))
            elif c.get("type") == "text" and str(c.get("text") or "").strip():
                events.append(("activity", make("respond", str(c["text"]), at=at)))
    elif role == "user" and isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error"):
                events.append(("error", c.get("tool_use_id")))
    return events


def _parse_codex(obj: dict[str, Any], cwd: str) -> list[Event]:
    p = obj.get("payload")
    if not isinstance(p, dict):
        return []
    at = str(obj.get("timestamp") or "")
    if obj.get("type") == "event_msg" and p.get("type") == "token_count":
        total = (p.get("info") or {}).get("total_token_usage") or {}
        if isinstance(total, dict):
            tokens = (int(total.get("input_tokens") or 0), int(total.get("output_tokens") or 0))
            return [("tokens_total", tokens)]
        return []
    if obj.get("type") == "response_item" and p.get("type") == "function_call":
        name = str(p.get("name") or "")
        try:
            args = json.loads(p.get("arguments") or "{}")
        except (TypeError, json.JSONDecodeError):
            args = {}
        events: list[Event] = []
        if name not in ("exec_command", "shell", "exec"):  # lệnh shell lấy từ CommandExecution
            events.append(("activity", tool_activity(name, args, cwd, at)))
        if name == "update_plan" and (todos := _todos(args)) is not None:
            events.append(("tasks", todos))
        return events
    if obj.get("type") != "event_msg" or p.get("type") not in ("item_started", "item_completed"):
        return []
    item = p.get("item")
    if not isinstance(item, dict):
        return []
    itype = str(item.get("type") or "")
    # Rollout chỉ ghi item khi đã xong (không có item_started): hoạt động hiện chậm một bước.
    if p.get("type") != "item_completed":
        return []
    if itype == "CommandExecution":
        act = _codex_command(item, cwd, at)
        if item.get("exit_code") not in (0, None):
            act["error"] = True
        return [("activity", act)]
    if itype == "AgentMessage" and item.get("phase") != "final_answer":
        text = " ".join(
            str(c.get("text") or "") for c in item.get("content") or [] if isinstance(c, dict)
        )
        return [("activity", make("respond", text, at=at))] if text.strip() else []
    if itype == "Reasoning":
        return [("activity", make("think", "Đang suy nghĩ", at=at))]
    if "Mcp" in itype:
        tool = str(item.get("tool") or item.get("name") or "")
        return [("activity", _aico_activity(tool, {}, at))]
    if "FileChange" in itype or "Patch" in itype:
        changes = item.get("changes")
        paths = list(changes) if isinstance(changes, dict) else []
        if isinstance(changes, list):
            paths = [str(c.get("path") or "") for c in changes if isinstance(c, dict)]
        text = f"Sửa {short_path(paths[0], cwd)}" if paths else "Sửa code"
        return [("activity", make("edit", text, tool="apply_patch", at=at))]
    return []


def _codex_command(item: dict[str, Any], cwd: str, at: str) -> dict[str, Any]:
    cmd = item.get("command")
    cmd = str(cmd[-1]) if isinstance(cmd, list) and cmd else str(cmd or "")
    parsed = item.get("parsed_cmd")
    first = parsed[0] if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict) else {}
    ptype = first.get("type")
    if ptype == "read" and first.get("path"):
        return make("read", f"Đọc {short_path(str(first['path']), cwd)}", tool="exec", at=at)
    if ptype in ("list_files", "search"):
        target = first.get("query") or first.get("path") or cmd
        return make("search", f"Tìm {target}", tool="exec", at=at)
    return _command_activity(cmd, "exec", at)


# ---------------------------------------------------------------- tìm transcript


def _slug(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def transcript_candidates(kind: str, home: Path, cwd: str) -> list[Path]:
    """Các file transcript có thể là của job (cwd riêng của job nên không lẫn job khác)."""
    if kind == "claude":
        return sorted((home / ".claude" / "projects" / _slug(cwd)).glob("*.jsonl"))
    if kind == "cursor":
        base = home / ".cursor" / "projects" / _slug(cwd).strip("-") / "agent-transcripts"
        return sorted(base.glob("*/*.jsonl"))
    if kind == "codex":
        return sorted((home / ".codex" / "sessions").glob("*/*/*/rollout-*.jsonl"))
    return []


def _codex_cwd(path: Path) -> str:
    try:
        with path.open(encoding="utf-8") as f:
            first = json.loads(f.readline())
    except (OSError, json.JSONDecodeError):
        return ""
    return str((first.get("payload") or {}).get("cwd") or "") if isinstance(first, dict) else ""


def find_transcript(kind: str, home: Path, cwd: str, since: float) -> Path | None:
    """Transcript mới nhất của job, được sửa sau lúc job bắt đầu."""
    best: tuple[float, Path] | None = None
    for p in transcript_candidates(kind, home, cwd):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime < since - 1 or (best and mtime <= best[0]):
            continue
        if kind == "codex" and _codex_cwd(p) != cwd:
            continue
        best = (mtime, p)
    return best[1] if best else None


# ---------------------------------------------------------------- theo dõi trong lúc chạy


class ActivityState:
    """Trạng thái gộp từ các sự kiện; snapshot() là phần thêm vào agent trong GET /status."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets = [s for s in secrets if s]
        self.activity: dict[str, Any] | None = None
        self.recent: list[dict[str, Any]] = []
        self.tasks: list[dict[str, str]] = []
        self.tool_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.source = ""

    def apply(self, events: Iterable[Event]) -> bool:
        changed = False
        for name, value in events:
            changed = True
            if name == "activity":
                act = {
                    k: mask(v, self._secrets) if isinstance(v, str) else v for k, v in value.items()
                }
                if act.get("tool"):
                    self.tool_calls += 1
                self.activity = act
                self.recent = [*self.recent, act][-RECENT_MAX:]
            elif name == "tasks":
                self.tasks = [{**t, "title": mask(t["title"], self._secrets)} for t in value]
            elif name == "tokens":
                self.tokens_in += value[0]
                self.tokens_out += value[1]
            elif name == "tokens_total":
                self.tokens_in, self.tokens_out = value
            elif name == "error" and (last := self.activity) is not None:
                failed: dict[str, Any] = {**last, "error": True}
                self.activity = failed
                if self.recent:
                    self.recent[-1] = failed
        return changed

    def snapshot(self) -> dict[str, Any]:
        done = sum(1 for t in self.tasks if t.get("status") == "completed")
        return {
            "activity": self.activity,
            "recent": list(self.recent),
            "tasks": list(self.tasks),
            "tasks_done": done,
            "stats": {
                "tool_calls": self.tool_calls,
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
            },
            "activity_source": self.source or None,
        }


class ActivityTracker:
    """Luồng phụ: tìm transcript của job, đọc phần mới mỗi `every` giây, báo qua on_update."""

    def __init__(
        self,
        kind: str,
        home: Path,
        cwd: Path,
        on_update: Callable[[dict[str, Any]], None],
        *,
        secrets: Iterable[str] = (),
        every: float = 1.0,
        heartbeat: Path | None = None,
    ) -> None:
        self.kind, self.home, self.cwd = kind, home, str(cwd)
        self.state = ActivityState(secrets)
        self._on_update = on_update
        self._every = every
        self._heartbeat = heartbeat
        self._since = time.time()
        self._path: Path | None = None
        self._pos = 0
        self._buf = ""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> ActivityTracker:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self.poll()  # đọc nốt phần cuối

    def _loop(self) -> None:
        while not self._stop.wait(self._every):
            with contextlib.suppress(Exception):  # theo dõi lỗi không được làm hỏng job
                self.poll()

    def poll(self) -> None:
        if self._path is None:
            self._path = find_transcript(self.kind, self.home, self.cwd, self._since)
            if self._path is None:
                return
            self.state.source = "transcript"
        try:
            with self._path.open("rb") as f:
                f.seek(self._pos)
                chunk = f.read()
        except OSError:
            return
        if not chunk:
            return
        self._pos += len(chunk)
        self._buf += chunk.decode("utf-8", errors="replace")
        *lines, self._buf = self._buf.split("\n")
        events: list[Event] = []
        for line in lines:
            with contextlib.suppress(json.JSONDecodeError):
                events += parse_line(self.kind, json.loads(line), self.cwd)
        if self.state.apply(events):
            if self._heartbeat is not None:
                with contextlib.suppress(OSError):
                    self._heartbeat.touch()
            self._on_update(self.state.snapshot())
