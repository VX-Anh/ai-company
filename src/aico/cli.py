"""aico migrate | check-config | serve | check-selfimprove | eval | recall"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import logging
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import uvicorn

from aico.api import create_api_app, with_api_prefix
from aico.container import build_container, setup_checkpointer
from aico.db import migrate
from aico.orchestrator import expire_overdue_approvals
from aico.registry import RegistryError, load_company
from aico.settings import Settings

log = logging.getLogger("aico")


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    # psycopg async cần SelectorEventLoop trên Windows.
    if sys.platform == "win32":
        return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(coro)


async def _migrate(settings: Settings) -> None:
    applied = await migrate(settings.database_url)
    await setup_checkpointer(settings.database_url)
    print("đã áp migration:", ", ".join(applied) or "(không có gì mới)")


async def _eval(settings: Settings, args: argparse.Namespace, suite: list[dict[str, Any]]) -> int:
    """aico eval start|report: chạy lại bộ ticket chuẩn với một biến thể (Phòng R&D, R1)."""
    import json

    from aico.db import create_pool
    from aico.rnd import evalrun
    from aico.tickets.postgres import PostgresTicketStore

    pool = create_pool(settings.database_url)
    await pool.open()
    try:
        store = PostgresTicketStore(pool)
        if args.eval_cmd == "start":
            created = await evalrun.start(store, suite, args.variant, args.repeat)
            for tid, new in created:
                print(tid, "mới" if new else "đã có")
            return 0
        observed = {str(i["id"]): i.get("observed") or {} for i in suite if "id" in i}
        result = await evalrun.report(store, args.variant, observed=observed, close=args.close)
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    finally:
        await pool.close()


async def _recall(database_url: str, args: argparse.Namespace) -> int:
    """aico recall: recall@k của tìm kiếm tri thức trên bộ câu hỏi chuẩn (Phòng R&D, GĐ2)."""
    import json

    from aico.db import create_pool
    from aico.rnd import recall
    from aico.rnd.cards import cards_from_dir
    from aico.rnd.store import KnowledgeStore

    questions = recall.load_questions(Path(args.questions))
    pool = create_pool(database_url)
    await pool.open()
    try:
        store = KnowledgeStore(pool)
        if args.cards:
            cards, skipped = cards_from_dir(Path(args.cards))
            for reason in skipped:
                print(f"bỏ qua thẻ: {reason}", file=sys.stderr)
            if not cards:
                raise recall.RecallError(f"không có thẻ nào trong {args.cards}/cards")
            await store.rebuild(cards, commit="local", skipped=skipped)
        results = [
            await recall.measure(store, questions, k=args.k, mode=mode) for mode in recall.MODES
        ]
    finally:
        await pool.close()
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
    else:
        print(recall.format_report(results, args.min))
    # Chỉ lượt của tool search_knowledge quyết định đạt/không; inject là số tham khảo.
    score = results[0]["recall"]
    return 0 if score is not None and score >= args.min else 1


def _recall_offline(args: argparse.Namespace) -> int:
    """Đo trên một bản checkout kho tri thức: dựng Postgres tạm bằng pgserver (gói dev), không
    đụng chỉ mục đang chạy."""
    import importlib
    import tempfile

    try:
        pgserver: Any = importlib.import_module("pgserver")
    except ImportError:
        print("LỖI: --cards cần pgserver (uv sync, nhóm dev); image aico không có", file=sys.stderr)
        return 1
    server = pgserver.get_server(
        Path(tempfile.mkdtemp(prefix="aico-recall-")), cleanup_mode="delete"
    )
    try:
        url = server.get_uri()
        _run(migrate(url))
        return _run(_recall(url, args))
    finally:
        server.cleanup()


async def _deadlines(container: Any, every: float = 60) -> None:
    while True:
        try:
            for ticket_id in await expire_overdue_approvals(container.tickets):
                log.info("ticket %s quá hạn duyệt", ticket_id)
        except Exception:
            log.exception("kiểm tra hạn duyệt lỗi")
        await asyncio.sleep(every)


async def _serve(settings: Settings) -> None:
    async with build_container(settings) as c:
        api = uvicorn.Server(
            uvicorn.Config(
                with_api_prefix(create_api_app(c.api)),
                host=settings.api_host,
                port=settings.api_port,
                log_level="info",
            )
        )
        relay = uvicorn.Server(
            uvicorn.Config(
                c.relay_app,
                host=settings.relay_host,
                port=settings.relay_port,
                log_level="info",
            )
        )
        bridge = uvicorn.Server(
            uvicorn.Config(
                c.bridge_app,
                host=settings.bridge_host,
                port=settings.bridge_port,
                log_level="info",
            )
        )
        tasks = [
            asyncio.create_task(api.serve(), name="api"),
            asyncio.create_task(relay.serve(), name="relay"),
            asyncio.create_task(bridge.serve(), name="bridge"),
            *(
                asyncio.create_task(r.run_forever(), name=f"runner-{i}")
                for i, r in enumerate(c.runners)
            ),
            asyncio.create_task(_deadlines(c), name="deadlines"),
        ]
        if c.poller is not None:
            tasks.append(
                asyncio.create_task(c.poller.run_forever(settings.poll_seconds), name="intake")
            )
        if c.site_watcher is not None:
            tasks.append(
                asyncio.create_task(
                    c.site_watcher.run_forever(settings.site_watch_seconds), name="site-watch"
                )
            )
        if c.librarian is not None:
            tasks.append(
                asyncio.create_task(
                    c.librarian.run_forever(settings.knowledge_watch_seconds), name="librarian"
                )
            )
        if c.scan_scheduler is not None:
            tasks.append(asyncio.create_task(c.scan_scheduler.run_forever(), name="rnd-scan"))
        if c.jira_sync is not None:
            tasks.append(
                asyncio.create_task(c.jira_sync.run_forever(settings.poll_seconds), name="jira")
            )
        log.info(
            "aico chạy: API %s:%d, relay %s:%d, tool bridge %s:%d, %d runner, planner %s",
            settings.api_host,
            settings.api_port,
            settings.relay_host,
            settings.relay_port,
            settings.bridge_host,
            settings.bridge_port,
            len(c.runners),
            settings.planner,
        )
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception() is not None:
                    raise t.exception()  # type: ignore[misc]
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aico")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="áp migration và tạo bảng checkpoint")
    check = sub.add_parser("check-config", help="kiểm tra thư mục company/")
    check.add_argument("path", nargs="?", default="company")
    sub.add_parser("serve", help="chạy API, model relay, runner, intake, đồng bộ Jira")
    guard = sub.add_parser(
        "check-selfimprove", help="kiểm PR tự cải tiến (nhánh rnd/*) chỉ sửa prompt/workers/limits"
    )
    guard.add_argument("--base", default="origin/main", help="ref đích của PR")
    guard.add_argument("--head", default="HEAD", help="ref của PR (chỉ đọc object Git)")
    ev = sub.add_parser("eval", help="chạy lại bộ ticket chuẩn với một biến thể (Phòng R&D)")
    ev_sub = ev.add_subparsers(dest="eval_cmd", required=True)
    ev_start = ev_sub.add_parser("start", help="tạo ticket eval từ bộ chuẩn (JSON của /rnd/bench)")
    ev_start.add_argument("--variant", required=True)
    ev_start.add_argument("--suite", required=True, help="file JSON hoặc - (stdin)")
    ev_start.add_argument("--repeat", type=int, default=2, help="số lần chạy mỗi ticket")
    ev_report = ev_sub.add_parser("report", help="kết quả của biến thể")
    ev_report.add_argument("--variant", required=True)
    ev_report.add_argument("--suite", help="bộ chuẩn để so với số liệu chạy thật")
    ev_report.add_argument("--close", action="store_true", help="đóng ticket đã chấm")
    rc = sub.add_parser("recall", help="recall@k của tìm kiếm tri thức trên bộ câu hỏi chuẩn")
    rc.add_argument("--questions", default="company/rnd/recall-questions.yaml")
    rc.add_argument("--cards", help="bản checkout kho tri thức: đo ngoại tuyến bằng Postgres tạm")
    rc.add_argument("--k", type=int, default=5)
    rc.add_argument("--min", type=float, default=0.8, help="dưới ngưỡng này thì mã thoát 1")
    rc.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")  # console Windows mặc định cp1252
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.cmd == "check-config":
        try:
            company = load_company(Path(args.path))
        except RegistryError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"OK: {len(company.roles)} vai trò, {len(company.sops)} SOP")
        return 0
    if args.cmd == "check-selfimprove":
        from aico.rnd.guardrails import check as check_selfimprove

        problems = check_selfimprove(Path.cwd(), args.base, args.head)
        for line in problems:
            print(line, file=sys.stderr)
        print("OK" if not problems else f"LỖI: {len(problems)} thay đổi ngoài phạm vi")
        return 1 if problems else 0
    if args.cmd == "recall":
        try:
            if args.cards:
                return _recall_offline(args)
            return _run(_recall(Settings.from_env().database_url, args))
        except ValueError as exc:  # RecallError: bộ câu hỏi hoặc tham số sai
            print(f"LỖI: {exc}", file=sys.stderr)
            return 1
    settings = Settings.from_env()
    if args.cmd == "eval":
        import json

        try:
            suite: list[dict[str, Any]] = []
            if args.suite:
                text = (
                    sys.stdin.read() if args.suite == "-" else Path(args.suite).read_text("utf-8")
                )
                suite = json.loads(text) if text.strip() else []  # stdin trống: không có bộ chuẩn
            return _run(_eval(settings, args, suite))
        except ValueError as exc:  # EvalError, JSON hỏng
            print(f"LỖI: {exc}", file=sys.stderr)
            return 1
    if args.cmd == "migrate":
        _run(_migrate(settings))
    else:
        _run(_serve(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
