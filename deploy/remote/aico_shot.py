#!/usr/bin/env python3
"""aico-shot: dựng bản xem trước của thư mục hiện tại rồi chụp ảnh bằng Chromium headless.

Dùng trong worker: `aico-shot --query hour=11 --query hour=21`. Mã nguồn đi tới remote runner
(dựng như bản xem trước của MR: `npm run build -- --base=./`, lấy `dist`), Chromium trong worker
mở bản đó qua mạng nội bộ ở mỗi cỡ màn hình × mỗi query, lưu PNG vào `.aico-shots/` của thư mục
hiện tại và in báo cáo: mã HTTP, lỗi console/trang/request, FPS (headless, chỉ tham khảo).
`--url` bỏ qua bước dựng và chụp thẳng một địa chỉ.

Agent mở PNG bằng tool đọc file/xem ảnh của mình để tự xem giao diện.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

# aico_remote.py nằm cạnh file này (/opt trong image, deploy/remote trong repo).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from aico_remote import EXIT_UNAVAILABLE, _heartbeat, pack  # noqa: E402

EXIT_BUILD = 2
DEFAULT_SIZES = ("1440x900", "390x844")
SHOT_DIR = ".aico-shots"
SCRIPT = Path(__file__).resolve().parent / "aico_shot.cjs"


def parse_size(raw: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{2,4})x(\d{2,4})", raw.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"cỡ màn hình dạng RỘNGxCAO, vd 1440x900: {raw}")
    return int(match.group(1)), int(match.group(2))


def preview_name(ticket: str) -> str:
    """Tên bản xem trước riêng của lần chụp; dựng lại cùng ticket thì ghi đè, không tích lại."""
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", ticket)[:50].strip("-.")
    return f"shot-{slug}" if slug else f"shot-{uuid.uuid4().hex[:12]}"


def plan_shots(
    base: str, sizes: list[tuple[int, int]], queries: list[str], out_dir: Path
) -> list[dict[str, Any]]:
    """Mỗi cỡ màn hình × mỗi query một ảnh; tên file đọc được (1440x900-hour-11.png)."""
    shots: list[dict[str, Any]] = []
    for query in queries or [""]:
        query = query.lstrip("?")
        url = base if not query else f"{base}{'&' if '?' in base else '?'}{query}"
        tag = re.sub(r"[^A-Za-z0-9]+", "-", query).strip("-")[:40]
        for width, height in sizes:
            name = f"{width}x{height}" + (f"-{tag}" if tag else "")
            shots.append(
                {"url": url, "width": width, "height": height, "path": str(out_dir / f"{name}.png")}
            )
    return shots


def build_preview(remote: str, token: str, name: str, args: argparse.Namespace) -> str:
    """Dựng bản xem trước trên máy build; trả địa chỉ nội bộ (không qua tên miền công khai)."""
    job = {
        "name": name,
        "archive": base64.b64encode(pack(Path.cwd())).decode("ascii"),
        "build": args.build,
        "output": args.output,
        "timeout_seconds": args.timeout,
        "meta": {"ticket": os.environ.get("AICO_TICKET_ID", ""), "kind": "aico-shot"},
    }
    request = urllib.request.Request(
        f"{remote}/preview",
        data=json.dumps(job).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=args.timeout + 120) as resp:
        result = json.loads(resp.read())
    if not result.get("ok"):
        error = str(result.get("error") or "dựng bản xem trước lỗi")
        raise RuntimeError(f"{error}\n{str(result.get('output', ''))[-3000:]}".strip())
    return f"{remote}/preview/{name}/"


def take_shots(shots: list[dict[str, Any]], wait_ms: int) -> list[dict[str, Any]]:
    """Chạy aico_shot.cjs (playwright-core + Chromium của hệ thống), trả kết quả từng ảnh."""
    spec = {
        "chromium": os.environ.get("AICO_CHROMIUM", "/usr/bin/chromium"),
        "wait": wait_ms,
        "shots": shots,
    }
    env = {
        **os.environ,
        "NODE_PATH": os.environ.get("AICO_NODE_PATH", "/usr/local/lib/node_modules"),
    }
    proc = subprocess.run(
        ["node", str(SCRIPT)],
        input=json.dumps(spec),
        capture_output=True,
        text=True,
        env=env,
        timeout=60 + len(shots) * (wait_ms / 1000 + 40),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Chromium lỗi: {proc.stderr.strip()[-1000:]}")
    return json.loads(proc.stdout)


def report(results: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in results:
        head = f"{item['path']}  ({item['width']}x{item['height']}, {item['url']})"
        if item.get("error"):
            lines.append(f"[LỖI] {head}\n  {item['error']}")
            continue
        lines.append(f"{head}\n  HTTP {item.get('status')}, ~{item.get('fps')} FPS (headless)")
        errors = item.get("errors") or []
        lines.extend(f"  ! {err}" for err in errors)
        if not errors:
            lines.append("  không có lỗi console/trang/request")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aico-shot", description="Dựng bản xem trước và chụp ảnh giao diện."
    )
    parser.add_argument("--url", help="chụp địa chỉ này, bỏ qua bước dựng")
    parser.add_argument("--size", action="append", type=parse_size, help="RỘNGxCAO, lặp được")
    parser.add_argument("--query", action="append", default=[], help="query thêm vào URL, lặp được")
    parser.add_argument("--wait", type=int, default=3000, help="ms chờ sau khi tải (mặc định 3000)")
    parser.add_argument("--build", default="npm run build -- --base=./", help="lệnh dựng")
    parser.add_argument("--output", default="dist", help="thư mục kết quả dựng (mặc định dist)")
    parser.add_argument("--timeout", type=float, default=900, help="giây chờ dựng (mặc định 900)")
    args = parser.parse_args(argv)
    sizes = args.size or [parse_size(s) for s in DEFAULT_SIZES]
    out_dir = Path(os.environ.get("AICO_SHOT_DIR") or Path.cwd() / SHOT_DIR)

    base = args.url
    if not base:
        remote = os.environ.get("AICO_REMOTE_URL", "").rstrip("/")
        token_file = os.environ.get("AICO_REMOTE_AUTH_FILE", "")
        if not remote or not token_file:
            print("[aico-shot] máy build từ xa chưa được cấu hình", file=sys.stderr)
            return EXIT_UNAVAILABLE
        token = Path(token_file).read_text(encoding="utf-8").strip()
        name = preview_name(os.environ.get("AICO_TICKET_ID", ""))
        stop = threading.Event()
        threading.Thread(target=_heartbeat, args=(stop,), daemon=True).start()
        print(f"[aico-shot] dựng bản xem trước: {args.build}", file=sys.stderr, flush=True)
        try:
            base = build_preview(remote, token, name, args)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:1500]
            print(f"[aico-shot] dựng lỗi {exc.code}: {detail}", file=sys.stderr)
            return EXIT_BUILD if exc.code in (400, 503) else EXIT_UNAVAILABLE
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"[aico-shot] không gọi được máy build: {exc}", file=sys.stderr)
            return EXIT_UNAVAILABLE
        except RuntimeError as exc:
            print(f"[aico-shot] dựng lỗi: {exc}", file=sys.stderr)
            return EXIT_BUILD
        finally:
            stop.set()

    out_dir.mkdir(parents=True, exist_ok=True)
    shots = plan_shots(base, sizes, args.query, out_dir)
    try:
        results = take_shots(shots, max(0, args.wait))
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"[aico-shot] không chụp được: {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    print(report(results))
    print(f"[aico-shot] {len(results)} ảnh trong {out_dir}; mở PNG để xem giao diện.")
    return 1 if any(item.get("error") for item in results) else 0


if __name__ == "__main__":
    sys.exit(main())
