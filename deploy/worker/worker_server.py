"""Worker server: nhận việc từ aico, chạy CLI chính thức của subscription ở chế độ headless.

Chạy trong container worker (chỉ thư viện chuẩn). Mỗi lần một việc; đang bận trả 409.

Biến môi trường:
- WORKER_KIND: claude | codex | cursor
- WORKER_TOKEN_FILE: token chung với aico (header Authorization: Bearer)
- WORKER_PORT (mặc định 8400)

POST /run {prompt, system_prompt?, mcp_url, mcp_token, mode: read|write, timeout_seconds?,
           ticket_id?, role?}
-> {ok, result, usage, duration_seconds} hoặc {ok: false, error, limited?}
GET /status -> trạng thái (idle | working | limited), việc đang làm, lượt chạy, token, hạn mức
"""

from __future__ import annotations

import base64
import contextlib
import hmac
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MCP_NAME = "aico"
CLAUDE_READ_ONLY = "--tools="  # tắt mọi tool có sẵn, chỉ còn tool MCP của aico


def _mcp_servers(url: str, token: str) -> dict[str, Any]:
    return {
        "mcpServers": {
            MCP_NAME: {"type": "http", "url": url, "headers": {"Authorization": f"Bearer {token}"}}
        }
    }


CLAUDE_WRITE_TOOLS = "Read,Edit,Write,Glob,Grep,Bash"
CLAUDE_REVIEW_TOOLS = "Read,Glob,Grep,Bash"  # review: đọc và chạy build/test, không sửa file
MAX_CHANGED_FILE_BYTES = 1_000_000
# File cấu hình của worker trong thư mục repo: không tính là thay đổi của agent.
LOCAL_EXCLUDES = (".cursor/",)


def build_command(
    job: dict[str, Any], kind: str, workdir: Path, cwd: Path | None = None
) -> tuple[list[str], dict[str, str]]:
    """Dòng lệnh và biến môi trường thêm cho từng CLI.

    workdir: thư mục tạm của job (file phụ như .last-message). cwd: thư mục CLI chạy (repo khi
    job có workspace), nơi Cursor đọc .cursor/.
    """
    prompt = job["prompt"]
    system = job.get("system_prompt") or ""
    mode = job.get("mode", "read")  # read | write | review
    read_only = mode == "read"
    url, token = job["mcp_url"], job["mcp_token"]
    if kind == "claude":
        extra = {"read": "", "write": CLAUDE_WRITE_TOOLS, "review": CLAUDE_REVIEW_TOOLS}[mode]
        allowed = f"mcp__{MCP_NAME}" + (f",{extra}" if extra else "")
        cmd = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "json",
            "--mcp-config",
            json.dumps(_mcp_servers(url, token)),
            "--strict-mcp-config",
            "--allowedTools",
            allowed,
            # dontAsk: tool ngoài danh sách bị từ chối, không treo chờ người bấm.
            "--permission-mode",
            "dontAsk",
        ]
        if read_only:
            cmd.append(CLAUDE_READ_ONLY)
        if system:
            cmd += ["--append-system-prompt", system]
        return cmd, {}
    if kind == "codex":
        full = f"{system}\n\n{prompt}" if system else prompt
        cmd = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox",
            "read-only" if read_only else "workspace-write",
            "-o",
            str(workdir / ".last-message"),
            "-c",
            f"mcp_servers.{MCP_NAME}.url={json.dumps(url)}",
            "-c",
            f'mcp_servers.{MCP_NAME}.bearer_token_env_var="AICO_TASK_TOKEN"',
            "-c",
            # Tool của aico đã qua guard + audit: không cần Codex hỏi duyệt từng lời gọi.
            f'mcp_servers.{MCP_NAME}.default_tools_approval_mode="approve"',
        ]
        if not read_only:
            # Cho cài dependency và chạy build/test trong sandbox của Codex.
            cmd += ["-c", "sandbox_workspace_write.network_access=true"]
        cmd.append(full)
        return cmd, {"AICO_TASK_TOKEN": token}
    if kind == "cursor":
        cursor_dir = (cwd or workdir) / ".cursor"
        cursor_dir.mkdir(exist_ok=True)
        servers = _mcp_servers(url, token)
        servers["mcpServers"][MCP_NAME].pop("type")
        (cursor_dir / "mcp.json").write_text(json.dumps(servers), encoding="utf-8")
        # Cho phép gọi tool MCP của aico mà không hỏi (đã qua guard + audit phía aico).
        permissions = {"permissions": {"allow": [f"Mcp({MCP_NAME}:*)"], "deny": []}}
        (cursor_dir / "cli.json").write_text(json.dumps(permissions), encoding="utf-8")
        full = f"{system}\n\n{prompt}" if system else prompt
        cmd = ["cursor-agent", "-p", full, "--output-format", "json", "--trust", "--approve-mcps"]
        # ask: chỉ đọc. Ghi code: --force cho chạy lệnh (build/test) trong thư mục repo tạm.
        cmd += ["--mode", "ask"] if read_only else ["--force"]
        return cmd, {}
    raise ValueError(f"WORKER_KIND không hỗ trợ: {kind}")


def parse_output(kind: str, stdout: str, workdir: Path) -> tuple[bool, str, dict[str, Any]]:
    if kind == "codex":
        last = workdir / ".last-message"
        text = last.read_text(encoding="utf-8").strip() if last.exists() else ""
        return bool(text), text or stdout[-2000:], {}
    try:
        data = json.loads(stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return False, stdout[-2000:], {}
    return (not data.get("is_error", False)), str(data.get("result", "")), data.get("usage") or {}


def token_usage(kind: str, usage: dict[str, Any], stdout: str) -> tuple[int, int]:
    """(input, output) token theo định dạng riêng của từng CLI."""
    if kind == "codex":
        match = re.search(r"tokens used\s*\n?\s*([\d,]+)", stdout)
        return (int(match[1].replace(",", "")) if match else 0), 0
    if kind == "cursor":
        return int(usage.get("inputTokens", 0)), int(usage.get("outputTokens", 0))
    inp = sum(
        int(usage.get(k, 0) or 0)
        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    return inp, int(usage.get("output_tokens", 0) or 0)


_LIMIT = re.compile(r"(usage|session|rate|weekly) limit|hit your .*limit|quota", re.IGNORECASE)
_RESETS_AT = re.compile(
    r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*(?:\(([^)]+)\))?", re.I
)
_TRY_IN = re.compile(r"try again in\s+((?:\d+\s*(?:days?|hours?|minutes?|mins?)[\s,and]*)+)", re.I)


def detect_limit(text: str, now: datetime) -> dict[str, Any] | None:
    """Lỗi hết hạn mức subscription -> {message, resets_at (ISO hoặc None)}."""
    if not text or not _LIMIT.search(text):
        return None
    resets: datetime | None = None
    if match := _RESETS_AT.search(text):
        hour = int(match[1]) % 12 + (12 if match[3].lower() == "pm" else 0)
        try:
            tz = ZoneInfo(match[4]) if match[4] else UTC
        except (ZoneInfoNotFoundError, ValueError):
            tz = UTC
        local = now.astimezone(tz)
        resets = local.replace(hour=hour, minute=int(match[2] or 0), second=0, microsecond=0)
        if resets <= local:
            resets += timedelta(days=1)
    elif match := _TRY_IN.search(text):
        units = {"day": "days", "hour": "hours", "min": "minutes"}
        delta = timedelta()
        for amount, unit in re.findall(r"(\d+)\s*(day|hour|min)", match[1], re.I):
            delta += timedelta(**{units[unit.lower()]: int(amount)})
        resets = now + delta
    return {
        "message": text.strip()[:300],
        "resets_at": resets.astimezone(UTC).isoformat() if resets else None,
        "detected_at": now.isoformat(),
    }


class WorkerStats:
    """Thống kê của worker, lưu trong volume home để giữ qua restart. Dùng cho GUI."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self.data: dict[str, Any] = {
            "runs_ok": 0,
            "runs_failed": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "day": "",
            "runs_today": 0,
            "tokens_today": 0,
            "last_run_at": None,
            "last_duration_seconds": None,
            "last_error": None,
            "limit": None,
        }
        with contextlib.suppress(OSError, json.JSONDecodeError):
            self.data.update(json.loads(path.read_text(encoding="utf-8")))
        # Agent con đang chạy trong phòng provider này: id -> {role, ticket_id, mode, started_at}
        self.agents: dict[str, dict[str, Any]] = {}
        self._seq = 0

    def limited_until(self, now: datetime) -> str | None:
        limit = self.data.get("limit")
        if not limit or not limit.get("resets_at"):
            return None
        return limit["resets_at"] if datetime.fromisoformat(limit["resets_at"]) > now else None

    def start(self, job: dict[str, Any], now: datetime) -> str:
        with self._lock:
            self._seq += 1
            agent_id = f"{job.get('role') or 'agent'}-{self._seq}"
            self.agents[agent_id] = {
                "id": agent_id,
                "ticket_id": job.get("ticket_id"),
                "role": job.get("role"),
                "mode": job.get("mode", "read"),
                "started_at": now.isoformat(),
            }
            return agent_id

    def finish(
        self, agent_id: str, result: dict[str, Any], tokens: tuple[int, int], now: datetime
    ) -> None:
        with self._lock:
            self.agents.pop(agent_id, None)
            d = self.data
            today = now.date().isoformat()
            if d["day"] != today:
                d.update(day=today, runs_today=0, tokens_today=0)
            d["runs_ok" if result["ok"] else "runs_failed"] += 1
            d["runs_today"] += 1
            d["input_tokens"] += tokens[0]
            d["output_tokens"] += tokens[1]
            d["tokens_today"] += sum(tokens)
            d["last_run_at"] = now.isoformat()
            d["last_duration_seconds"] = result.get("duration_seconds")
            d["last_error"] = None if result["ok"] else str(result.get("error", ""))[:300]
            limit = None if result["ok"] else detect_limit(str(result.get("error", "")), now)
            if result["ok"] or limit is not None:
                d["limit"] = limit  # chạy được là hết bị giới hạn
            with contextlib.suppress(OSError):
                self._path.write_text(json.dumps(d), encoding="utf-8")

    def snapshot(self, kind: str, slots: int, now: datetime) -> dict[str, Any]:
        """Trạng thái phòng provider: hạn mức chung, các agent con đang chạy, thống kê."""
        with self._lock:
            agents = sorted(self.agents.values(), key=lambda a: a["started_at"])
            limited = self.limited_until(now)
            state = "limited" if limited else ("working" if agents else "idle")
            return {
                "kind": kind,
                "state": state,
                "slots": slots,
                "agents": agents,
                "current": agents[0] if agents else None,  # tương thích GUI cũ
                **self.data,
            }


def extract_workspace(archive_b64: str, target: Path) -> None:
    """Giải nén tar.gz của repo (GitHub/GitLab có một thư mục gốc) vào target.

    Bỏ thư mục gốc; chỉ nhận file và thư mục thường; từ chối đường dẫn thoát ra ngoài.
    """
    data = base64.b64decode(archive_b64)
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts[1:]
            if not parts or not (member.isfile() or member.isdir()):
                continue
            dest = (target / Path(*parts)).resolve()
            if not dest.is_relative_to(root):
                raise ValueError(f"đường dẫn không hợp lệ trong archive: {member.name}")
            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is not None:
                dest.write_bytes(src.read())


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=aico", "-c", "user.email=aico@localhost", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def snapshot_repo(repo: Path) -> None:
    """Commit gốc để sau này so ra thay đổi của agent (tôn trọng .gitignore của repo)."""
    _git(repo, "init", "-q")
    (repo / ".git" / "info" / "exclude").write_text("\n".join(LOCAL_EXCLUDES) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")


def collect_changes(repo: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Thay đổi so với commit gốc: [{path, action, content}] và danh sách file bỏ qua."""
    _git(repo, "add", "-A")
    changes: list[dict[str, Any]] = []
    skipped: list[str] = []
    for line in _git(
        repo, "diff", "--cached", "--name-status", "--no-renames", "HEAD"
    ).splitlines():
        status, _, path = line.partition("\t")
        if status.startswith("D"):
            changes.append({"path": path, "action": "delete"})
            continue
        file = repo / path
        if file.stat().st_size > MAX_CHANGED_FILE_BYTES:
            skipped.append(f"{path} (quá lớn)")
            continue
        try:
            content = file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            skipped.append(f"{path} (file nhị phân)")
            continue
        action = "create" if status.startswith("A") else "update"
        changes.append({"path": path, "action": action, "content": content})
    return changes, skipped


def run_job(job: dict[str, Any], kind: str) -> dict[str, Any]:
    workdir = Path(tempfile.mkdtemp(prefix="job-"))
    started = time.monotonic()
    repo: Path | None = None
    try:
        if job.get("workspace"):
            repo = workdir / "repo"
            extract_workspace(job["workspace"]["data"], repo)
            snapshot_repo(repo)
        cmd, extra_env = build_command(job, kind, workdir, repo)
        proc = subprocess.run(
            cmd,
            cwd=repo or workdir,
            env={**os.environ, **extra_env},
            capture_output=True,
            text=True,
            timeout=float(job.get("timeout_seconds", 1800)),
            check=False,
        )
        ok, result, usage = parse_output(kind, proc.stdout, workdir)
        if proc.returncode != 0:
            ok = False
            result = result or proc.stderr[-2000:]
        out: dict[str, Any] = {
            "ok": ok,
            "result": result if ok else "",
            "error": "" if ok else (result or proc.stderr[-2000:] or f"exit {proc.returncode}"),
            "usage": usage,
            "tokens": token_usage(kind, usage, proc.stdout),
            "duration_seconds": round(time.monotonic() - started, 1),
        }
        if repo is not None and ok and job.get("mode") == "write":
            out["changes"], out["skipped"] = collect_changes(repo)
        return out
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "hết thời gian", "result": "", "usage": {}, "tokens": (0, 0)}
    except (subprocess.CalledProcessError, tarfile.TarError, OSError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        error = f"chuẩn bị workspace lỗi: {detail}"[:500]
        return {"ok": False, "error": error, "result": "", "usage": {}, "tokens": (0, 0)}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


class _Handler(BaseHTTPRequestHandler):
    kind = ""
    token = ""
    slots = 2  # số agent con chạy song song trong phòng provider
    seats = threading.BoundedSemaphore(2)
    stats = WorkerStats(Path("/dev/null"))

    def _reply(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        got = self.headers.get("Authorization", "")
        return hmac.compare_digest(got.encode(), f"Bearer {self.token}".encode())

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._reply(200, {"kind": self.kind, "agents": len(self.stats.agents)})
        elif self.path == "/status":
            if not self._authorized():
                self._reply(401, {"error": "unauthorized"})
                return
            self._reply(200, self.stats.snapshot(self.kind, self.slots, _now()))
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/run":
            self._reply(404, {"error": "not found"})
            return
        if not self._authorized():
            self._reply(401, {"error": "unauthorized"})
            return
        if not self.seats.acquire(blocking=False):
            self._reply(409, {"error": "phòng đã đủ agent"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            job = json.loads(self.rfile.read(length))
            if until := self.stats.limited_until(_now()):
                # Chưa tới giờ mở lại hạn mức: từ chối ngay, không gọi CLI.
                self._reply(
                    200, {"ok": False, "limited": True, "error": f"hết hạn mức tới {until}"}
                )
                return
            agent_id = self.stats.start(job, _now())
            result = run_job(job, self.kind)
            self.stats.finish(agent_id, result, tuple(result.pop("tokens")), _now())
            self._reply(200, {**result, "agent_id": agent_id})
        except (ValueError, KeyError) as exc:
            self._reply(400, {"error": str(exc)})
        finally:
            self.seats.release()


def _now() -> datetime:
    return datetime.now(UTC)


def main() -> None:
    _Handler.kind = os.environ["WORKER_KIND"]
    _Handler.token = Path(os.environ["WORKER_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    _Handler.slots = max(1, int(os.environ.get("WORKER_SLOTS", "2")))
    _Handler.seats = threading.BoundedSemaphore(_Handler.slots)
    _Handler.stats = WorkerStats(
        Path(os.environ.get("WORKER_STATS_FILE", Path.home() / ".aico-worker-stats.json"))
    )
    port = int(os.environ.get("WORKER_PORT", "8400"))
    ThreadingHTTPServer(("0.0.0.0", port), _Handler).serve_forever()


if __name__ == "__main__":
    main()
