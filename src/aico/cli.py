"""aico migrate | check-config | serve | check-selfimprove | eval"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import logging
import sys
from collections.abc import Coroutine, Mapping
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
    """aico eval start|report|grade|cleanup: chạy lại bộ ticket chuẩn (Phòng R&D, R1)."""
    import json

    import httpx

    from aico.agents.remote import ExecSettings, RemoteExec
    from aico.db import create_pool
    from aico.governance import PostgresApprovalStore, PostgresAuditLog
    from aico.rnd import evalrun
    from aico.tickets.postgres import PostgresTicketStore
    from aico.tools import Guard, McpToolClient, ToolGateway

    pool = create_pool(settings.database_url)
    await pool.open()
    try:
        store = PostgresTicketStore(pool)
        if args.eval_cmd == "start":
            created = await evalrun.start(store, suite, args.variant, args.repeat)
            for tid, new in created:
                print(tid, "mới" if new else "đã có")
            return 0
        if args.eval_cmd == "cleanup":
            # Gateway rút gọn: chỉ server Git, vẫn đủ guard + audit như aico serve.
            company = load_company(settings.company_dir)
            gateway = ToolGateway(
                company,
                Guard(company.policy, PostgresApprovalStore(pool)),
                McpToolClient({"gitlab": settings.mcp_gitlab_url}),
                PostgresAuditLog(pool),
            )
            rows = await evalrun.cleanup(
                store,
                args.variant,
                evalrun.gateway_deleter(gateway),
                default_project=settings.default_project,
                dry_run=args.dry_run,
            )
            print(json.dumps(rows, ensure_ascii=False, indent=1))
            return 1 if any("error" in r for r in rows) else 0
        if args.eval_cmd == "grade":
            if not settings.remote_url or not settings.worker_token:
                raise evalrun.EvalError("chấm acceptance cần máy build (AICO_REMOTE_URL)")
            company = load_company(settings.company_dir)
            gateway = ToolGateway(
                company,
                Guard(company.policy, PostgresApprovalStore(pool)),
                McpToolClient({"gitlab": settings.mcp_gitlab_url}),
                PostgresAuditLog(pool),
            )
            async with httpx.AsyncClient() as client:
                remote = RemoteExec(
                    ExecSettings(settings.remote_url, settings.worker_token, client),
                    gateway,
                    role_id=company.sop("fix_ticket").steps["code"],
                )

                async def run_acceptance(
                    ticket_id: str,
                    project: str,
                    ref: str,
                    commands: Mapping[str, str],
                    image: str | None,
                ) -> dict[str, dict[str, Any]]:
                    return await remote.run(ticket_id, project, ref, commands, image=image)

                rows = await evalrun.grade(
                    store,
                    args.variant,
                    suite,
                    run_acceptance,
                    default_project=settings.default_project,
                    regrade=args.regrade,
                )
            print(json.dumps(rows, ensure_ascii=False, indent=1))
            return 1 if any("error" in r for r in rows) else 0
        observed = {str(i["id"]): i.get("observed") or {} for i in suite if "id" in i}
        need_grade = frozenset(
            str(i["id"]) for i in suite if i.get("id") and evalrun.acceptance_of(i)
        )
        result = await evalrun.report(
            store, args.variant, observed=observed, close=args.close, need_grade=need_grade
        )
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    finally:
        await pool.close()


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
    ev_grade = ev_sub.add_parser(
        "grade", help="chấm acceptance (build/test trên máy build) cho ticket đã tới chờ merge"
    )
    ev_grade.add_argument("--variant", required=True)
    ev_grade.add_argument("--suite", required=True, help="bộ chuẩn có acceptance: {build, test}")
    ev_grade.add_argument("--regrade", action="store_true", help="chấm lại ticket đã chấm")
    ev_clean = ev_sub.add_parser(
        "cleanup", help="xoá nhánh ai/<id>, base/<id> của ticket chạy lại đã đóng"
    )
    ev_clean.add_argument("--variant", required=True)
    ev_clean.add_argument("--dry-run", action="store_true", help="chỉ liệt kê, không xoá")
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
    settings = Settings.from_env()
    if args.cmd == "eval":
        import json

        try:
            suite: list[dict[str, Any]] = []
            if getattr(args, "suite", None):
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
