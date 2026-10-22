#!/usr/bin/env python3
"""aico-remote: chạy build/test của thư mục hiện tại trên máy build từ xa (server).

Dùng trong worker: `aico-remote 'npm run build && npm test'`. Dependency được cài sẵn theo
lockfile (npm ci, uv sync) ở pha có mạng; lệnh của bạn chạy không có mạng.

Gửi mã nguồn (bỏ .git, node_modules, dist, .venv...) tới remote runner của aico, in log và
thoát với mã thoát của lệnh. Trong lúc chờ, chạm file heartbeat để watchdog của worker
không coi agent là treo.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import tarfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

EXCLUDE_DIRS = {
    ".git",
    "node_modules",
    "dist",
    ".venv",
    "__pycache__",
    ".cursor",
    ".pytest_cache",
    ".aico-shots",
}
EXIT_UNAVAILABLE = 3


def pack(root: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if path.is_file() and not path.is_symlink():
                    tar.add(path, arcname=str(path.relative_to(root)), recursive=False)
    return buf.getvalue()


def _heartbeat(stop: threading.Event) -> None:
    path = os.environ.get("AICO_HEARTBEAT_FILE")
    if not path:
        return
    while not stop.wait(20):
        try:
            Path(path).touch()
        except OSError:
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aico-remote", description="Chạy build/test trên máy build từ xa."
    )
    parser.add_argument("command", help="lệnh shell, ví dụ 'npm run build && npm test'")
    parser.add_argument("--image", help="môi trường chạy (mặc định: node)")
    parser.add_argument("--timeout", type=float, default=900, help="giây (mặc định 900)")
    args = parser.parse_args(argv)
    url = os.environ.get("AICO_REMOTE_URL", "").rstrip("/")
    # Tên biến không chứa TOKEN/KEY/SECRET: Codex lọc các biến đó khỏi môi trường của lệnh shell.
    token_file = os.environ.get("AICO_REMOTE_AUTH_FILE", "")
    if not url or not token_file:
        print("[aico-remote] máy build từ xa chưa được cấu hình", file=sys.stderr)
        return EXIT_UNAVAILABLE
    token = Path(token_file).read_text(encoding="utf-8").strip()
    job = {
        "command": args.command,
        "image": args.image,
        "timeout_seconds": args.timeout,
        "archive": base64.b64encode(pack(Path.cwd())).decode("ascii"),
    }
    request = urllib.request.Request(
        f"{url}/exec",
        data=json.dumps(job).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    stop = threading.Event()
    threading.Thread(target=_heartbeat, args=(stop,), daemon=True).start()
    print(f"[aico-remote] chạy trên máy build: {args.command}", file=sys.stderr, flush=True)
    try:
        with urllib.request.urlopen(request, timeout=args.timeout + 120) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        print(f"[aico-remote] lỗi {exc.code}: {detail}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"[aico-remote] không gọi được máy build: {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    finally:
        stop.set()
    if result.get("truncated"):
        print("[aico-remote] (log dài, chỉ giữ phần cuối)")
    print(
        result.get("output", ""), end="" if str(result.get("output", "")).endswith("\n") else "\n"
    )
    code = int(result.get("exit_code", 1))
    print(f"[aico-remote] xong sau {result.get('duration_seconds', '?')} giây, exit {code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
