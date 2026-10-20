"""aico migrate | check-config | serve"""

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

from aico.api import create_api_app
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
                create_api_app(c.api),
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
            asyncio.create_task(c.runner.run_forever(), name="runner"),
            asyncio.create_task(_deadlines(c), name="deadlines"),
        ]
        if c.poller is not None:
            tasks.append(
                asyncio.create_task(c.poller.run_forever(settings.poll_seconds), name="intake")
            )
        log.info(
            "aico chạy: API %s:%d, relay %s:%d, tool bridge %s:%d, runner %s, planner %s",
            settings.api_host,
            settings.api_port,
            settings.relay_host,
            settings.relay_port,
            settings.bridge_host,
            settings.bridge_port,
            c.runner.owner,
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
    sub.add_parser("serve", help="chạy API, model relay, runner, intake")
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
    settings = Settings.from_env()
    if args.cmd == "migrate":
        _run(_migrate(settings))
    else:
        _run(_serve(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
