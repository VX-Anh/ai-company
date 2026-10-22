"""Máy build từ xa: chạy build/test và dựng bản xem trước GUI trên server qua SSH + Docker.

Chạy trên PC (chỉ thư viện chuẩn + ssh). Server chỉ làm compute: nhận mã nguồn qua stdin của
ssh, chạy trong container dùng một lần, không nhận secret nào. Mỗi lần chạy có hai pha trên
cùng một volume tạm:
- setup (có mạng): giải nén, cài dependency theo lockfile (npm ci, uv sync);
- run (không mạng): lệnh build/test của agent, hoặc lệnh build bản xem trước.

Biến môi trường:
- REMOTE_SSH_TARGET (vd aico@14.161.5.155), REMOTE_SSH_KEY_FILE, REMOTE_KNOWN_HOSTS_FILE
- RUNNER_TOKEN_FILE: token chung với aico và worker (header Authorization: Bearer)
- REMOTE_IMAGES: tên=image, cách nhau dấu phẩy; tên đầu tiên là mặc định
- REMOTE_CPUS, REMOTE_MEMORY, REMOTE_CONCURRENCY, REMOTE_MAX_TIMEOUT
- PREVIEW_DIR, PREVIEW_PUBLIC_URL (địa chỉ người mở bản xem trước), RUNNER_PORT (mặc định 8500)
- PREVIEW_HOSTS: tên miền=bản xem trước, vd office.aico.io.vn=aico-office: tên miền đó phục vụ
  bản xem trước ở gốc "/" (bản main của GUI). Request tới tên miền công khai chỉ được GET.

POST /exec {command, archive (tar.gz base64), strip?, image?, timeout_seconds?}
  -> {exit_code, output, truncated, duration_seconds}
POST /preview {name, archive, strip?, image?, build?, output?, meta?} -> {url, output, ...}
POST /shots {name: img-..., files: [{name: *.png, data: base64}]} -> {files: [{name, url}]}
GET /preview/ (danh sách), GET /preview/<tên>/... (file tĩnh), GET /health
"""

from __future__ import annotations

import base64
import contextlib
import hmac
import io
import json
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_CHARS = 60_000
# Ảnh chụp giao diện của reviewer (aico-shot), gắn vào ticket: giới hạn số lượng, cỡ và tuổi.
MAX_SHOTS = 16
MAX_SHOT_BYTES = 4 * 1024 * 1024
SHOT_KEEP_SECONDS = 30 * 86400
SHOT_PREFIX = "img-"
SHOT_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}\.png$")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_PREVIEW_BYTES = 200 * 1024 * 1024
LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "remote-runner"}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DEFAULT_BUILD = "npm run build -- --base=./"
# Cài dependency theo lockfile có sẵn; repo không có lockfile thì bỏ qua.
SETUP_SCRIPT = (
    "set -e; tar xzf - --no-same-owner {strip}; "
    "if [ -f package-lock.json ] && command -v npm >/dev/null; then "
    "npm ci --no-audit --no-fund; fi; "
    "if [ -f uv.lock ] && command -v uv >/dev/null; then uv sync --frozen; fi"
)


def _env_file(name: str) -> str:
    path = os.environ.get(name, "")
    return Path(path).read_text(encoding="utf-8").strip() if path else ""


def parse_images(raw: str) -> dict[str, str]:
    images: dict[str, str] = {}
    for part in raw.split(","):
        key, _, image = part.strip().partition("=")
        if key and image:
            images[key.strip()] = image.strip()
    return images


class Config:
    def __init__(self, env: dict[str, str] | None = None) -> None:
        env = dict(os.environ if env is None else env)
        self.target = env.get("REMOTE_SSH_TARGET", "")
        self.key_file = env.get("REMOTE_SSH_KEY_FILE", "")
        self.known_hosts = env.get("REMOTE_KNOWN_HOSTS_FILE", "")
        self.images = parse_images(env.get("REMOTE_IMAGES", "node=node:22-bookworm-slim"))
        # Docker rootless chỉ giới hạn được CPU khi systemd giao controller cpu; trống = không đặt.
        self.cpus = env.get("REMOTE_CPUS", "")
        self.memory = env.get("REMOTE_MEMORY", "6g")
        self.max_timeout = float(env.get("REMOTE_MAX_TIMEOUT", "1200"))
        self.preview_dir = Path(env.get("PREVIEW_DIR", "/data/previews"))
        self.public_url = env.get("PREVIEW_PUBLIC_URL", "http://localhost:8500").rstrip("/")
        self.hosts = {
            h.lower(): name for h, name in parse_images(env.get("PREVIEW_HOSTS", "")).items()
        }
        public = urlsplit(self.public_url).hostname or ""
        # Tên miền đi qua tunnel ra Internet: không nhận POST (chạy lệnh, dựng bản xem trước).
        self.public_hosts = set(self.hosts) | ({public} if public not in LOCAL_HOSTS else set())


def container_flags(cfg: Config, name: str, volume: str) -> list[str]:
    """Giới hạn chung của container trên server: không quyền đặc biệt, có trần tài nguyên."""
    return [
        "--rm",
        "--name",
        name,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "1024",
        *(["--cpus", cfg.cpus] if cfg.cpus else []),
        "--memory",
        cfg.memory,
        "-v",
        f"{volume}:/w",
        "-v",
        "aico-cache:/cache",
        "-e",
        "npm_config_cache=/cache/npm",
        "-e",
        "UV_CACHE_DIR=/cache/uv",
        "-e",
        "CI=1",
        "-w",
        "/w",
    ]


def remote_script(
    cfg: Config,
    run_id: str,
    image: str,
    command: str,
    strip: int,
    *,
    preview_output: str | None = None,
) -> str:
    """Script sh chạy trên server. Mã nguồn (tar.gz) đi vào stdin của pha setup.

    preview_output: thư mục build cần lấy về; khi có, stdout chỉ chứa tar.gz của thư mục đó,
    log của cả hai pha đi ra stderr.
    """
    volume = f"aico-ws-{run_id}"
    setup = SETUP_SCRIPT.format(strip=f"--strip-components={strip}" if strip else "")
    setup_cmd = shlex.join(
        [
            "docker",
            "run",
            "-i",
            *container_flags(cfg, f"aico-setup-{run_id}", volume),
            image,
            "sh",
            "-c",
            setup,
        ]
    )
    if preview_output is None:
        run = f"set -e; {command}"
    else:
        out = shlex.quote(preview_output)
        run = f"set -e; ({command}) 1>&2; test -d {out}; tar czf - -C {out} ."
    run_cmd = shlex.join(
        [
            "docker",
            "run",
            "--network",
            "none",
            *container_flags(cfg, f"aico-run-{run_id}", volume),
            image,
            "sh",
            "-c",
            run,
        ]
    )
    # Chạy lệnh: gộp stderr vào stdout để log giữ đúng thứ tự. Xem trước: stdout dành cho tar.
    redirect = " 1>&2" if preview_output is not None else ""
    merge = "exec 2>&1; " if preview_output is None else ""
    return (
        f"{merge}v={volume}; docker volume create $v >/dev/null || exit 125; "
        "trap 'docker volume rm -f $v >/dev/null 2>&1' EXIT; "
        f"{setup_cmd}{redirect} || exit $?; "
        f"{run_cmd} </dev/null"
    )


def ssh_command(cfg: Config, script: str) -> list[str]:
    return [
        "ssh",
        "-i",
        cfg.key_file,
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={cfg.known_hosts}",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=30",
        cfg.target,
        shlex.join(["sh", "-c", script]),
    ]


def _cleanup(cfg: Config, run_id: str) -> None:
    """Hết giờ: ssh bị giết nhưng container trên server vẫn chạy, phải dọn riêng."""
    names = [f"aico-setup-{run_id}", f"aico-run-{run_id}"]
    script = (
        f"docker rm -f {' '.join(names)} >/dev/null 2>&1; "
        f"docker volume rm -f aico-ws-{run_id} >/dev/null 2>&1; true"
    )
    with contextlib.suppress(subprocess.SubprocessError, OSError):
        subprocess.run(ssh_command(cfg, script), capture_output=True, timeout=60, check=False)


def run_remote(
    cfg: Config, script: str, run_id: str, archive: bytes, timeout: float
) -> tuple[int, bytes, bytes]:
    proc = subprocess.Popen(
        ssh_command(cfg, script),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = proc.communicate(archive, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        _cleanup(cfg, run_id)
        raise
    return proc.returncode, stdout, stderr


def tail(text: str, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    return (text, False) if len(text) <= limit else (text[-limit:], True)


def safe_extract(data: bytes, target: Path) -> None:
    """Giải nén bản build: chỉ file/thư mục thường, không thoát khỏi target, có trần dung lượng."""
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.isfile():
                total += member.size
                if total > MAX_PREVIEW_BYTES:
                    raise ValueError("bản build quá lớn")
            elif not member.isdir():
                raise ValueError(f"bản build có mục không phải file: {member.name}")
        tar.extractall(target, filter="data")


class Runner:
    def __init__(self, cfg: Config, concurrency: int = 4) -> None:
        self.cfg = cfg
        self._slots = threading.BoundedSemaphore(concurrency)
        self._preview_lock = threading.Lock()

    def _image(self, name: str | None) -> str:
        key = name or next(iter(self.cfg.images))
        if key not in self.cfg.images:
            raise ValueError(f"image không hỗ trợ: {key} (có: {', '.join(self.cfg.images)})")
        return self.cfg.images[key]

    def _timeout(self, value: Any) -> float:
        return max(30.0, min(float(value or self.cfg.max_timeout), self.cfg.max_timeout))

    def _run(self, script_for: Any, archive: bytes, timeout: float) -> tuple[int, bytes, bytes]:
        if not self._slots.acquire(timeout=600):
            raise RuntimeError("máy build đang bận, thử lại sau")
        try:
            run_id = uuid.uuid4().hex[:12]
            return run_remote(self.cfg, script_for(run_id), run_id, archive, timeout)
        finally:
            self._slots.release()

    def exec(self, job: dict[str, Any]) -> dict[str, Any]:
        command = str(job.get("command") or "").strip()
        if not command:
            raise ValueError("thiếu command")
        archive = decode_archive(job)
        image = self._image(job.get("image"))
        strip = int(job.get("strip") or 0)
        started = time.monotonic()
        try:
            code, stdout, stderr = self._run(
                lambda rid: remote_script(self.cfg, rid, image, command, strip),
                archive,
                self._timeout(job.get("timeout_seconds")),
            )
        except subprocess.TimeoutExpired:
            return {
                "exit_code": 124,
                "output": "hết thời gian, đã dừng container",
                "truncated": False,
            }
        output = (stdout + stderr).decode("utf-8", "replace")
        if code == 255:
            output += "\n[aico-remote] không kết nối được server qua SSH"
        text, truncated = tail(output)
        return {
            "exit_code": code,
            "output": text,
            "truncated": truncated,
            "duration_seconds": round(time.monotonic() - started, 1),
        }

    def preview(self, job: dict[str, Any]) -> dict[str, Any]:
        name = str(job.get("name") or "")
        if not NAME_RE.match(name):
            raise ValueError("tên bản xem trước không hợp lệ")
        output_dir = str(job.get("output") or "dist")
        out_path = PurePosixPath(output_dir)  # đường dẫn trong container Linux
        if out_path.is_absolute() or ".." in out_path.parts:
            raise ValueError("output phải là đường dẫn tương đối trong repo")
        build = str(job.get("build") or DEFAULT_BUILD)
        archive = decode_archive(job)
        image = self._image(job.get("image"))
        strip = int(job.get("strip") or 0)
        started = time.monotonic()
        try:
            code, stdout, stderr = self._run(
                lambda rid: remote_script(
                    self.cfg, rid, image, build, strip, preview_output=output_dir
                ),
                archive,
                self._timeout(job.get("timeout_seconds")),
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "hết thời gian build bản xem trước"}
        log, _ = tail(stderr.decode("utf-8", "replace"), 8000)
        if code != 0 or not stdout:
            return {"ok": False, "error": f"build lỗi (exit {code})", "output": log}
        self.cfg.preview_dir.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=self.cfg.preview_dir))
        try:
            safe_extract(stdout, staging)
            meta = {
                "name": name,
                "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "duration_seconds": round(time.monotonic() - started, 1),
                **{
                    k: str(v)
                    for k, v in dict(job.get("meta") or {}).items()
                    if k not in ("name", "built_at")
                },
            }
            with self._preview_lock:
                final = self.cfg.preview_dir / name
                old = self.cfg.preview_dir / f".{name}.old"
                shutil.rmtree(old, ignore_errors=True)
                if final.exists():
                    final.rename(old)
                staging.rename(final)
                shutil.rmtree(old, ignore_errors=True)
                (self.cfg.preview_dir / f"{name}.json").write_text(
                    json.dumps(meta, ensure_ascii=False), encoding="utf-8"
                )
        except (ValueError, tarfile.TarError, OSError) as exc:
            shutil.rmtree(staging, ignore_errors=True)
            return {"ok": False, "error": f"không giải nén được bản build: {exc}", "output": log}
        return {"ok": True, "url": f"{self.cfg.public_url}/preview/{name}/", "output": log, **meta}

    def shots(self, job: dict[str, Any]) -> dict[str, Any]:
        """Lưu ảnh PNG vào thư mục img-...; phục vụ như file tĩnh của bản xem trước.

        Không có file .json nên không hiện trong danh sách bản xem trước. Mỗi lần ghi đè cả thư
        mục; thư mục ảnh cũ hơn SHOT_KEEP_SECONDS bị xoá.
        """
        name = str(job.get("name") or "")
        if not name.startswith(SHOT_PREFIX) or not NAME_RE.match(name):
            raise ValueError(f"tên thư mục ảnh phải bắt đầu bằng {SHOT_PREFIX}")
        files = job.get("files")
        if not isinstance(files, list) or not files or len(files) > MAX_SHOTS:
            raise ValueError(f"cần 1..{MAX_SHOTS} ảnh")
        decoded: dict[str, bytes] = {}
        for item in files:
            file = str(dict(item).get("name") or "")
            if not SHOT_FILE_RE.match(file):
                raise ValueError(f"tên ảnh không hợp lệ: {file[:80]}")
            try:
                data = base64.b64decode(str(item.get("data") or ""), validate=True)
            except ValueError as exc:
                raise ValueError(f"ảnh {file} không phải base64") from exc
            if not data.startswith(PNG_MAGIC) or len(data) > MAX_SHOT_BYTES:
                raise ValueError(f"ảnh {file} không phải PNG hoặc quá lớn")
            decoded[file] = data
        self.cfg.preview_dir.mkdir(parents=True, exist_ok=True)
        self._prune_shots()
        staging = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=self.cfg.preview_dir))
        for file, data in decoded.items():
            (staging / file).write_bytes(data)
        with self._preview_lock:
            final = self.cfg.preview_dir / name
            shutil.rmtree(final, ignore_errors=True)
            staging.rename(final)
        base = f"{self.cfg.public_url}/preview/{name}/"
        return {"ok": True, "files": [{"name": f, "url": base + f} for f in decoded]}

    def _prune_shots(self) -> None:
        cutoff = time.time() - SHOT_KEEP_SECONDS
        for path in self.cfg.preview_dir.glob(f"{SHOT_PREFIX}*"):
            with contextlib.suppress(OSError):
                if path.is_dir() and path.stat().st_mtime < cutoff:
                    shutil.rmtree(path)

    def previews(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if not self.cfg.preview_dir.is_dir():
            return items
        for meta_file in sorted(self.cfg.preview_dir.glob("*.json")):
            with contextlib.suppress(OSError, ValueError):
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                if (self.cfg.preview_dir / meta_file.stem).is_dir():
                    items.append(
                        {**meta, "url": f"{self.cfg.public_url}/preview/{meta_file.stem}/"}
                    )
        return sorted(items, key=lambda m: m.get("built_at", ""), reverse=True)


def decode_archive(job: dict[str, Any]) -> bytes:
    try:
        data = base64.b64decode(str(job.get("archive") or ""), validate=True)
    except ValueError as exc:
        raise ValueError("archive không phải base64") from exc
    if not data:
        raise ValueError("thiếu archive")
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError(f"archive quá lớn ({len(data)} byte)")
    return data


def static_path(root: Path, name: str, rest: str) -> Path | None:
    """File tĩnh của bản xem trước; None nếu tên sai hoặc đường dẫn thoát khỏi thư mục."""
    if not NAME_RE.match(name):
        return None
    base = (root / name).resolve()
    path = (base / rest).resolve()
    if not path.is_relative_to(base):
        return None
    if path.is_dir():
        path = path / "index.html"
    return path if path.is_file() else None


def index_html(items: list[dict[str, Any]]) -> str:
    rows = "".join(
        f'<li><a href="{escape(i["url"])}">{escape(i["name"])}</a> · {escape(i.get("ref", ""))}'
        f" · {escape(i.get('built_at', ''))}</li>"
        for i in items
    )
    return (
        "<!doctype html><meta charset=utf-8><title>Bản xem trước</title>"
        f"<h1>Bản xem trước</h1><ul>{rows or '<li>chưa có</li>'}</ul>"
    )


class Handler(BaseHTTPRequestHandler):
    runner: Runner
    token: str

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json")

    def _host(self) -> str:
        return (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()

    def _file(self, name: str, rest: str) -> None:
        file = static_path(self.runner.cfg.preview_dir, name, rest.lstrip("/"))
        if file is None:
            return self._send(404, b"not found", "text/plain")
        kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        return self._send(200, file.read_bytes(), kind)

    def _redirect(self, location: str) -> None:
        self.send_response(301)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - tên do http.server quy định
        path = self.path.split("?", 1)[0]
        site = self.runner.cfg.hosts.get(self._host())
        if site is not None and not path.startswith("/preview"):
            return self._file(site, path)
        if path == "/" and self._host() in self.runner.cfg.public_hosts:
            return self._redirect("/preview/")
        if path == "/health":
            return self._json(200, {"ok": True, "target": bool(self.runner.cfg.target)})
        if path in ("/preview", "/preview/"):
            items = self.runner.previews()
            if "application/json" in (self.headers.get("Accept") or ""):
                return self._json(200, {"previews": items})
            return self._send(200, index_html(items).encode(), "text/html; charset=utf-8")
        match = re.match(r"^/preview/([^/]+)(/.*)?$", path)
        if match:
            name, rest = match.group(1), match.group(2)
            if rest is None:
                return self._redirect(f"/preview/{name}/")
            return self._file(name, rest)
        return self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if self._host() in self.runner.cfg.public_hosts:
            return self._json(403, {"error": "chỉ gọi được từ mạng nội bộ"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_ARCHIVE_BYTES * 2:
            return self._json(413, {"error": "yêu cầu quá lớn"})
        # Đọc hết body trước khi trả lỗi: đóng kết nối khi client còn gửi làm client nhận
        # ConnectionAborted thay vì mã 401 (gặp trên Windows).
        body = self.rfile.read(length)
        auth = self.headers.get("Authorization", "")
        if not self.token or not hmac.compare_digest(auth, f"Bearer {self.token}"):
            return self._json(401, {"error": "sai token"})
        try:
            job = json.loads(body or b"{}")
            if self.path == "/exec":
                return self._json(200, self.runner.exec(job))
            if self.path == "/preview":
                return self._json(200, self.runner.preview(job))
            if self.path == "/shots":
                return self._json(200, self.runner.shots(job))
        except (ValueError, TypeError) as exc:
            return self._json(400, {"error": str(exc)})
        except RuntimeError as exc:
            return self._json(503, {"error": str(exc)})
        return self._json(404, {"error": "không có endpoint này"})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if not self.path.startswith("/preview/"):
            super().log_message(format, *args)


def prepare_key(src: str) -> str:
    """ssh từ chối khoá đọc được bởi nhóm (secret mode 640): chép ra file 600 của tiến trình."""
    if not src:
        return src
    fd, dest = tempfile.mkstemp(prefix="remote-key-")
    with os.fdopen(fd, "wb") as out:
        out.write(Path(src).read_bytes())
    os.chmod(dest, 0o600)
    return dest


def main() -> None:
    cfg = Config()
    cfg.key_file = prepare_key(cfg.key_file)
    Handler.runner = Runner(cfg, int(os.environ.get("REMOTE_CONCURRENCY", "4")))
    Handler.token = _env_file("RUNNER_TOKEN_FILE")
    port = int(os.environ.get("RUNNER_PORT", "8500"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"remote runner: {cfg.target or '(chưa đặt REMOTE_SSH_TARGET)'}, cổng {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
