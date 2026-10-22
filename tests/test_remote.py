"""Máy build từ xa: remote runner, lệnh aico-remote, bản xem trước MR."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import sys
import tarfile
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from aico.agents import PreviewError, PreviewSettings, RemotePreviewer
from aico.core import TicketStatus
from aico.registry import Company
from harness import PROJECT, make_harness

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "runner-token"


def _load(name: str, rel: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rr = _load("remote_runner", "deploy/remote/remote_runner.py")
ar = _load("aico_remote", "deploy/remote/aico_remote.py")
shot = _load("aico_shot", "deploy/remote/aico_shot.py")


def _tar_gz(files: dict[str, str], extra: list[tarfile.TarInfo] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tar.addfile(info)
    return buf.getvalue()


def _config(tmp_path: Path) -> Any:
    return rr.Config(
        {
            "REMOTE_SSH_TARGET": "aico@server",
            "REMOTE_IMAGES": "node=node:22,python=uv:py",
            "PREVIEW_DIR": str(tmp_path / "previews"),
            "PREVIEW_PUBLIC_URL": "http://localhost:8500",
        }
    )


def test_remote_script_runs_command_without_network(tmp_path: Path) -> None:
    script = rr.remote_script(_config(tmp_path), "abc", "node:22", "npm test", 1)
    setup, run = script.split("|| exit $?;")
    # Pha setup có mạng (npm ci), lệnh của agent chạy với --network none.
    assert "--network" not in setup and "npm ci" in setup and "--strip-components=1" in setup
    assert "--network none" in run and "npm test" in run
    for part in (setup, run):
        assert "--cap-drop ALL" in part and "no-new-privileges" in part and "aico-ws-abc:/w" in part
        assert "--memory 6g" in part and "--cpus" not in part
    assert script.startswith("exec 2>&1;")  # log giữ đúng thứ tự
    preview = rr.remote_script(
        _config(tmp_path), "abc", "node:22", "npm run build", 0, preview_output="dist"
    )
    assert "exec 2>&1" not in preview and "tar czf - -C dist ." in preview
    assert "--strip-components" not in preview


def test_ssh_command_pins_host_key(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.key_file, cfg.known_hosts = "/k", "/known"
    cmd = rr.ssh_command(cfg, "echo 'a b'")
    assert cmd[:3] == ["ssh", "-i", "/k"]
    assert "StrictHostKeyChecking=yes" in cmd and "UserKnownHostsFile=/known" in cmd
    assert cmd[-2] == "aico@server" and cmd[-1] == "sh -c 'echo '\"'\"'a b'\"'\"''"


@pytest.fixture
def server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """Runner thật trên cổng ngẫu nhiên; phần ssh tới server được thay bằng giả lập."""
    calls: list[dict[str, Any]] = []

    def fake_run_remote(
        cfg: Any, script: str, run_id: str, archive: bytes, timeout: float
    ) -> tuple[int, bytes, bytes]:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            names = sorted(m.name for m in tar.getmembers())
        calls.append({"script": script, "names": names, "timeout": timeout})
        if "tar czf - -C dist" in script:
            dist = _tar_gz({"./index.html": "<h1>preview</h1>", "./assets/app.js": "1"})
            return 0, dist, b"vite build ok\n"
        if "exit 7" in script:
            return 7, b"FAIL test\n", b""
        return 0, b"build ok\n", b""

    monkeypatch.setattr(rr, "run_remote", fake_run_remote)
    handler: Any = type(
        "H", (rr.Handler,), {"runner": rr.Runner(_config(tmp_path)), "token": TOKEN}
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", calls
    httpd.shutdown()


def _post(url: str, body: dict[str, Any], token: str = TOKEN) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_aico_remote_sends_workspace_and_returns_exit_code(
    server: tuple[str, list[dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url, calls = server
    repo = tmp_path / "repo"
    for rel in ("package.json", "src/a.ts", "node_modules/x/i.js", ".git/HEAD", "dist/b.js"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("x")
    secret = tmp_path / "token"
    secret.write_text(TOKEN)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AICO_REMOTE_URL", url)
    monkeypatch.setenv("AICO_REMOTE_AUTH_FILE", str(secret))

    assert ar.main(["npm run build"]) == 0
    assert calls[-1]["names"] == ["package.json", "src/a.ts"]  # bỏ node_modules, .git, dist
    assert "build ok" in capsys.readouterr().out
    assert ar.main(["npm test; exit 7"]) == 7  # mã thoát của lệnh đi thẳng về agent
    assert "FAIL test" in capsys.readouterr().out

    secret.write_text("sai")
    assert ar.main(["npm test"]) == ar.EXIT_UNAVAILABLE
    monkeypatch.delenv("AICO_REMOTE_URL")
    assert ar.main(["npm test"]) == ar.EXIT_UNAVAILABLE


def test_aico_shot_plans_names_and_sizes(tmp_path: Path) -> None:
    shots = shot.plan_shots(
        "http://r/preview/shot-T-1/",
        [(1440, 900), (390, 844)],
        ["?hour=11", "hour=21&x=1"],
        tmp_path,
    )
    assert [(s["url"], Path(s["path"]).name) for s in shots] == [
        ("http://r/preview/shot-T-1/?hour=11", "1440x900-hour-11.png"),
        ("http://r/preview/shot-T-1/?hour=11", "390x844-hour-11.png"),
        ("http://r/preview/shot-T-1/?hour=21&x=1", "1440x900-hour-21-x-1.png"),
        ("http://r/preview/shot-T-1/?hour=21&x=1", "390x844-hour-21-x-1.png"),
    ]
    assert shot.plan_shots("http://a/?demo", [(10, 20)], [], tmp_path)[0]["url"] == "http://a/?demo"
    assert shot.plan_shots("http://a/?demo", [(10, 20)], ["h=1"], tmp_path)[0]["url"] == (
        "http://a/?demo&h=1"
    )
    assert shot.preview_name("T-1") == "shot-T-1"
    assert shot.preview_name("../x y").startswith("shot-x-y")
    assert rr.NAME_RE.match(shot.preview_name("")) and rr.NAME_RE.match(shot.preview_name("é" * 99))
    with pytest.raises(Exception, match="RỘNGxCAO"):
        shot.parse_size("big")


def test_aico_shot_builds_preview_then_shoots_internal_url(
    server: tuple[str, list[dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url, calls = server
    repo = tmp_path / "repo"
    for rel in ("package.json", "src/a.ts", ".aico-shots/old.png"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("x")
    secret = tmp_path / "token"
    secret.write_text(TOKEN)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AICO_REMOTE_URL", url)
    monkeypatch.setenv("AICO_REMOTE_AUTH_FILE", str(secret))
    monkeypatch.setenv("AICO_TICKET_ID", "T-9")
    shot_calls: list[list[dict[str, Any]]] = []

    def fake_take(shots: list[dict[str, Any]], wait_ms: int) -> list[dict[str, Any]]:
        shot_calls.append(shots)
        # Chromium thật mở được bản xem trước qua địa chỉ nội bộ của runner.
        with urllib.request.urlopen(shots[0]["url"], timeout=5) as resp:
            assert resp.read() == b"<h1>preview</h1>"
        return [{**s, "status": 200, "fps": 60, "errors": []} for s in shots]

    monkeypatch.setattr(shot, "take_shots", fake_take)
    assert shot.main(["--query", "hour=11", "--size", "800x600"]) == 0
    assert calls[-1]["names"] == ["package.json", "src/a.ts"]  # không gửi ảnh cũ lên máy build
    assert shot_calls[-1][0]["url"] == f"{url}/preview/shot-T-9/?hour=11"
    assert shot_calls[-1][0]["path"] == str(repo / ".aico-shots" / "800x600-hour-11.png")
    out = capsys.readouterr().out
    assert "~60 FPS" in out and "không có lỗi" in out

    # --url: không dựng, chụp thẳng; ảnh lỗi -> exit 1 và báo lỗi.
    built = len(calls)
    monkeypatch.setattr(
        shot,
        "take_shots",
        lambda shots, wait: [
            {**s, "status": None, "errors": [], "error": "timeout"} for s in shots
        ],
    )
    assert shot.main(["--url", "http://x/"]) == 1
    assert len(calls) == built and "[LỖI]" in capsys.readouterr().out

    secret.write_text("sai")
    assert shot.main([]) == ar.EXIT_UNAVAILABLE
    monkeypatch.delenv("AICO_REMOTE_URL")
    assert shot.main([]) == ar.EXIT_UNAVAILABLE


def test_runner_rejects_bad_requests(server: tuple[str, list[dict[str, Any]]]) -> None:
    url, calls = server
    archive = base64.b64encode(_tar_gz({"a": "1"})).decode()
    assert _post(f"{url}/exec", {"command": "x", "archive": archive}, token="sai")[0] == 401
    assert _post(f"{url}/exec", {"command": "", "archive": archive})[0] == 400
    assert _post(f"{url}/exec", {"command": "x", "archive": archive, "image": "ruby"})[0] == 400
    for name, output in [("../x", "dist"), ("T-1", "../etc"), ("T-1", "/abs")]:
        body = {"name": name, "archive": archive, "output": output}
        assert _post(f"{url}/preview", body)[0] == 400
    assert calls == []


def test_preview_is_built_and_served(server: tuple[str, list[dict[str, Any]]]) -> None:
    url, _ = server
    archive = base64.b64encode(_tar_gz({"root/package.json": "{}"})).decode()
    body = {"name": "T-1", "archive": archive, "strip": 1, "meta": {"ref": "ai/T-1"}}
    status, result = _post(f"{url}/preview", body)
    assert status == 200 and result["ok"], result
    assert result["url"] == "http://localhost:8500/preview/T-1/"
    with urllib.request.urlopen(f"{url}/preview/T-1/", timeout=5) as resp:
        assert resp.read() == b"<h1>preview</h1>"
    with urllib.request.urlopen(f"{url}/preview/T-1/assets/app.js", timeout=5) as resp:
        assert resp.headers["Content-Type"] == "text/javascript"
    req = urllib.request.Request(f"{url}/preview/", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        items = json.loads(resp.read())["previews"]
    assert [(i["name"], i["ref"]) for i in items] == [("T-1", "ai/T-1")]
    # Dựng lại thay bản cũ, không để thư mục tạm.
    assert _post(f"{url}/preview", body)[1]["ok"]
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"{url}/preview/T-1/../../T-1.json", timeout=5)


PNG = rr.PNG_MAGIC + b"anh"


def _png(name: str, data: bytes = PNG) -> dict[str, str]:
    return {"name": name, "data": base64.b64encode(data).decode()}


def test_runner_stores_and_serves_shots(
    server: tuple[str, list[dict[str, Any]]], tmp_path: Path
) -> None:
    url, _ = server
    body = {"name": "img-T-1-1", "files": [_png("1440x900.png"), _png("390x844.png")]}
    status, result = _post(f"{url}/shots", body)
    assert status == 200 and result["ok"]
    assert result["files"][0] == {
        "name": "1440x900.png",
        "url": "http://localhost:8500/preview/img-T-1-1/1440x900.png",
    }
    with urllib.request.urlopen(f"{url}/preview/img-T-1-1/390x844.png", timeout=5) as resp:
        assert resp.read() == PNG and resp.headers["Content-Type"] == "image/png"
    # Không lẫn vào danh sách bản xem trước.
    req = urllib.request.Request(f"{url}/preview/", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert json.loads(resp.read())["previews"] == []
    # Ghi lại thay cả thư mục.
    assert _post(f"{url}/shots", {"name": "img-T-1-1", "files": [_png("a.png")]})[0] == 200
    assert sorted(p.name for p in (tmp_path / "previews" / "img-T-1-1").iterdir()) == ["a.png"]

    bad = [
        {"name": "T-1", "files": [_png("a.png")]},  # thiếu tiền tố img-
        {"name": "img-T-1", "files": []},
        {"name": "img-T-1", "files": [_png("../a.png")]},
        {"name": "img-T-1", "files": [_png("a.svg")]},
        {"name": "img-T-1", "files": [_png("a.png", b"<svg/>")]},  # không phải PNG
        {"name": "img-T-1", "files": [_png(f"{i}.png") for i in range(rr.MAX_SHOTS + 1)]},
    ]
    for item in bad:
        assert _post(f"{url}/shots", item)[0] == 400, item
    assert _post(f"{url}/shots", body, token="sai")[0] == 401


def test_runner_prunes_old_shots(tmp_path: Path) -> None:
    runner = rr.Runner(_config(tmp_path))
    old = tmp_path / "previews" / "img-T-old"
    keep = tmp_path / "previews" / "T-old"  # bản xem trước, không phải ảnh
    for path in (old, keep):
        path.mkdir(parents=True)
        os.utime(path, (0, 0))
    runner.shots({"name": "img-T-new", "files": [_png("a.png")]})
    assert not old.exists() and keep.exists()
    assert (tmp_path / "previews" / "img-T-new" / "a.png").read_bytes() == PNG


async def test_publish_shots_attaches_screenshots(company: Company) -> None:
    def runner(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/shots"
        job = json.loads(request.content)
        assert job["name"].startswith("img-T-1-") and job["files"][0]["name"] == "a.png"
        base = f"http://localhost:8500/preview/{job['name']}/"
        return httpx.Response(
            200, json={"ok": True, "files": [{"name": "a.png", "url": base + "a.png"}]}
        )

    h = make_harness(company)
    await h.new_ticket(labels=["low-risk"])
    await _previewer(h, runner).publish_shots("T-1", [_png("a.png")])
    shots = [a for a in (await h.tickets.get("T-1")).artifacts if a.kind == "screenshot"]
    assert len(shots) == 1 and shots[0].ref.endswith("/a.png")
    assert shots[0].url and shots[0].url.endswith("/a.png")

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "ảnh lỗi"})

    with pytest.raises(PreviewError, match="ảnh lỗi"):
        await _previewer(h, broken).publish_shots("T-1", [_png("b.png")])


def test_preview_build_rejects_links_and_escapes(tmp_path: Path) -> None:
    link = tarfile.TarInfo("./evil")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    with pytest.raises(ValueError, match="không phải file"):
        rr.safe_extract(_tar_gz({"./index.html": "x"}, [link]), tmp_path / "a")
    with pytest.raises(tarfile.TarError):
        rr.safe_extract(_tar_gz({"../out.txt": "x"}), tmp_path / "b")
    root = tmp_path / "p"
    (root / "T-1").mkdir(parents=True)
    (root / "secret.txt").write_text("x")
    assert rr.static_path(root, "T-1", "../secret.txt") is None
    assert rr.static_path(root, "..", "secret.txt") is None


def test_worker_heartbeat_keeps_waiting_agent_alive(tmp_path: Path) -> None:
    ws = _load("worker_server_hb", "deploy/worker/worker_server.py")
    beat = tmp_path / ".heartbeat"
    waiter = [
        sys.executable,
        "-c",
        "import os, time, pathlib\nfor _ in range(8):\n"
        "    pathlib.Path(os.environ['AICO_HEARTBEAT_FILE']).touch(); time.sleep(0.25)\n"
        "print('xong')",
    ]
    env = {**os.environ, "AICO_HEARTBEAT_FILE": str(beat)}
    work = tmp_path / "repo"
    work.mkdir()
    code, out, _ = ws.run_agent(
        waiter, work, env, timeout=20, idle_seconds=1, check_every=0.3, heartbeat=beat
    )
    assert code == 0 and out.strip() == "xong"


def _previewer(h: Any, handler: Any, projects: tuple[str, ...] = (PROJECT,)) -> RemotePreviewer:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = PreviewSettings(
        url="http://remote-runner:8500", token=TOKEN, http_client=client, projects=projects
    )
    return RemotePreviewer(settings, h.gateway, h.tickets)


async def test_preview_scheduled_after_merge_request(company: Company) -> None:
    jobs: list[dict[str, Any]] = []

    def runner(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        job = json.loads(request.content)
        jobs.append(job)
        return httpx.Response(
            200, json={"ok": True, "url": f"http://localhost:8500/preview/{job['name']}/"}
        )

    holder: dict[str, RemotePreviewer] = {}

    class Lazy:
        def schedule(self, ticket_id: str, project: str, branch: str) -> None:
            holder["p"].schedule(ticket_id, project, branch)

        def schedule_site(self, ticket_id: str, project: str, ref: str) -> None:
            holder["p"].schedule_site(ticket_id, project, ref)

    h = make_harness(company, previewer=Lazy())
    holder["p"] = _previewer(h, runner)
    await h.new_ticket(labels=["low-risk"])
    await h.runner.run_until_idle()
    for task in list(holder["p"]._tasks):
        await task

    t = await h.tickets.get("T-1")
    assert t.status is TicketStatus.AWAITING_MERGE_APPROVAL
    assert len(jobs) == 1 and jobs[0]["name"] == "T-1" and jobs[0]["strip"] == 1
    assert jobs[0]["meta"] == {"ticket": "T-1", "project": PROJECT, "ref": "ai/T-1"}
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(jobs[0]["archive"]))) as tar:
        assert tar.getmembers()  # mã nguồn branch tải qua MCP gitlab
    previews = [a for a in t.artifacts if a.kind == "preview"]
    assert [(a.ref, a.url) for a in previews] == [("ai/T-1", "http://localhost:8500/preview/T-1/")]

    # Merge xong: dựng lại bản chính của repo (tên theo repo), không gắn thêm vào ticket.
    await h.decide("T-1", "approved")
    await h.runner.run_until_idle()
    for task in list(holder["p"]._tasks):
        await task
    assert (await h.tickets.get("T-1")).status is TicketStatus.DONE
    assert [(j["name"], j["meta"]["ref"]) for j in jobs[1:]] == [("aico-office", "main")]
    t = await h.tickets.get("T-1")
    assert len([a for a in t.artifacts if a.kind == "preview"]) == 1


async def test_preview_errors_do_not_touch_ticket(company: Company) -> None:
    h = make_harness(company)
    await h.new_ticket(labels=["low-risk"])
    await h.runner.run_until_idle()

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": False, "error": "build lỗi (exit 1)", "output": "tsc: lỗi"}
        )

    with pytest.raises(PreviewError, match="tsc: lỗi"):
        await _previewer(h, failing).build("T-1", PROJECT, "ai/T-1")
    with pytest.raises(PreviewError, match="AICO_PREVIEW_PROJECTS"):
        await _previewer(h, failing, projects=()).build("T-1", PROJECT, "ai/T-1")
    off = _previewer(h, failing, projects=())
    off.schedule("T-1", PROJECT, "ai/T-1")  # repo không bật: không tạo việc nền
    assert not off._tasks
    assert not [a for a in (await h.tickets.get("T-1")).artifacts if a.kind == "preview"]


async def test_preview_api(company: Company) -> None:
    from aico.api import ApiDeps, create_api_app

    h = make_harness(company)

    async def preview(ticket_id: str, site: bool) -> dict[str, Any]:
        if ticket_id == "T-1":
            raise ValueError("ticket chưa có branch code")
        return {"ok": True, "url": "u", "site": site}

    await h.new_ticket()
    await h.new_ticket("T-2")
    app = create_api_app(ApiDeps(h.tickets, h.approvals, preview=preview))
    off = create_api_app(ApiDeps(h.tickets, h.approvals))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a") as c:
        assert (await c.post("/tickets/T-1/preview")).status_code == 409
        assert (await c.post("/tickets/T-2/preview")).json() == {
            "ok": True,
            "url": "u",
            "site": False,
        }
        assert (await c.post("/tickets/T-2/preview?site=true")).json()["site"] is True
        assert (await c.post("/tickets/T-9/preview")).status_code == 404
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=off), base_url="http://a") as c:
        assert (await c.post("/tickets/T-2/preview")).status_code == 501


async def test_api_prefix_serves_api_but_not_internal(company: Company) -> None:
    from aico.api import ApiDeps, create_api_app, with_api_prefix

    h = make_harness(company)
    await h.new_ticket()
    app = with_api_prefix(create_api_app(ApiDeps(h.tickets, h.approvals, internal_token="i")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a") as c:
        assert (await c.get("/tickets/T-1")).status_code == 200
        assert (await c.get("/api/tickets/T-1")).json()["id"] == "T-1"  # GUI cùng origin
        headers = {"Authorization": "Bearer i"}
        url = "/internal/approvals/x?project=p&mr_iid=1"
        assert (await c.get(url, headers=headers)).status_code == 200
        assert (await c.get("/api" + url, headers=headers)).status_code == 404
        assert (await c.get("/apix/tickets/T-1")).status_code == 404


def test_public_hosts_serve_site_and_refuse_post(tmp_path: Path) -> None:
    cfg = rr.Config(
        {
            "PREVIEW_DIR": str(tmp_path),
            "PREVIEW_PUBLIC_URL": "https://preview.aico.io.vn",
            "PREVIEW_HOSTS": "office.aico.io.vn=aico-office",
        }
    )
    (tmp_path / "aico-office" / "assets").mkdir(parents=True)
    (tmp_path / "aico-office" / "index.html").write_text("<h1>office</h1>")
    (tmp_path / "aico-office" / "assets" / "a.js").write_text("1")
    assert cfg.public_hosts == {"office.aico.io.vn", "preview.aico.io.vn"}
    handler: Any = type("H", (rr.Handler,), {"runner": rr.Runner(cfg), "token": TOKEN})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def get(path: str, host: str) -> tuple[int, bytes]:
        req = urllib.request.Request(base + path, headers={"Host": host})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, b""

    try:
        assert get("/", "office.aico.io.vn") == (200, b"<h1>office</h1>")
        assert get("/assets/a.js", "office.aico.io.vn:443") == (200, b"1")
        assert get("/preview/aico-office/", "preview.aico.io.vn")[1] == b"<h1>office</h1>"
        assert get("/", "localhost:8500")[0] == 404
        req = urllib.request.Request(
            base + "/exec",
            data=b"{}",
            headers={"Host": "preview.aico.io.vn", "Authorization": f"Bearer {TOKEN}"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=5)
        assert err.value.code == 403
    finally:
        httpd.shutdown()
