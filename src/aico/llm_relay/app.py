"""Model relay: endpoint tương thích OpenAI đứng trước 9router.

Agent và sandbox chỉ gọi được relay bằng token tạm theo task; chỉ relay giữ key 9router.
Relay chỉ chuyển tiếp, không sửa nội dung, ngoài việc bật include_usage khi streaming.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from aico.llm_relay.budget import (
    BudgetExceeded,
    BudgetLedger,
    Reservation,
    cost_usd,
    estimate_tokens,
    estimate_usd,
)
from aico.llm_relay.tokens import TaskGrant, TaskTokens
from aico.observability import Generation, TraceSink
from aico.registry import Company


@dataclass(frozen=True)
class RelayDeps:
    company: Company
    tokens: TaskTokens
    ledger: BudgetLedger
    upstream: httpx.AsyncClient  # base_url = 9router .../v1, header Authorization sẵn
    trace: TraceSink


def _error(status: int, message: str, kind: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": kind}}, status_code=status)


def _usage_of(data: Any) -> tuple[int, int] | None:
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return None
    return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


def create_relay_app(deps: RelayDeps) -> FastAPI:
    app = FastAPI(title="aico model relay", docs_url=None, redoc_url=None)

    def grant_of(authorization: str | None) -> TaskGrant | None:
        if not authorization or not authorization.lower().startswith("bearer "):
            return None
        return deps.tokens.resolve(authorization[7:].strip())

    async def finish(
        grant: TaskGrant,
        reservation: Reservation,
        model: str,
        used_model: str | None,
        usage: tuple[int, int] | None,
        started: float,
        status: int,
    ) -> None:
        if usage is None:
            if status >= 400:
                await deps.ledger.cancel(reservation)
                prompt, completion = 0, 0
            else:
                # Không có usage thì tính theo ước lượng, nghiêng về an toàn.
                await deps.ledger.settle(reservation, reservation.est_tokens, reservation.est_usd)
                prompt, completion = reservation.est_tokens, 0
            usd = 0.0 if status >= 400 else reservation.est_usd
        else:
            prompt, completion = usage
            usd = cost_usd(deps.company.policy, model, prompt, completion)
            await deps.ledger.settle(reservation, prompt + completion, usd)
        await deps.trace.generation(
            Generation(
                ticket_id=grant.ticket_id,
                role=grant.role,
                model_requested=model,
                model_used=used_model,
                prompt_tokens=prompt,
                completion_tokens=completion,
                cost_usd=usd,
                latency_ms=(time.monotonic() - started) * 1000,
                status=status,
            )
        )

    @app.get("/v1/models")
    async def models(authorization: str | None = Header(default=None)) -> Response:
        grant = grant_of(authorization)
        if grant is None:
            return _error(401, "token không hợp lệ hoặc đã hết hạn", "unauthorized")
        role = deps.company.role(grant.role)
        return JSONResponse(
            {"object": "list", "data": [{"id": m, "object": "model"} for m in role.models()]}
        )

    @app.post("/v1/chat/completions")
    async def chat(request: Request, authorization: str | None = Header(default=None)) -> Response:
        grant = grant_of(authorization)
        if grant is None:
            return _error(401, "token không hợp lệ hoặc đã hết hạn", "unauthorized")
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _error(400, "body không phải JSON", "invalid_request")
        role = deps.company.role(grant.role)
        model = str(body.get("model", ""))
        if model not in role.models():
            return _error(403, f"vai trò '{role.id}' không được dùng model '{model}'", "forbidden")

        est_tokens = estimate_tokens(body)
        try:
            reservation = await deps.ledger.reserve(
                grant.ticket_id,
                role,
                model,
                est_tokens,
                estimate_usd(deps.company.policy, model, est_tokens),
            )
        except BudgetExceeded as exc:
            return _error(429, str(exc), "budget_exceeded")

        started = time.monotonic()
        if not body.get("stream"):
            try:
                resp = await deps.upstream.post("chat/completions", json=body)
            except httpx.HTTPError as exc:
                await deps.ledger.cancel(reservation)
                return _error(502, f"9router lỗi: {exc}", "upstream_error")
            data: Any = None
            with contextlib.suppress(json.JSONDecodeError):
                data = resp.json()
            used = data.get("model") if isinstance(data, dict) else None
            await finish(
                grant, reservation, model, used, _usage_of(data), started, resp.status_code
            )
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "application/json"),
            )

        body = {**body, "stream_options": {**body.get("stream_options", {}), "include_usage": True}}
        upstream_req = deps.upstream.build_request("POST", "chat/completions", json=body)
        try:
            resp = await deps.upstream.send(upstream_req, stream=True)
        except httpx.HTTPError as exc:
            await deps.ledger.cancel(reservation)
            return _error(502, f"9router lỗi: {exc}", "upstream_error")

        async def stream() -> AsyncIterator[bytes]:
            usage: tuple[int, int] | None = None
            used: str | None = None
            buffer = b""
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
                    buffer += chunk
                    *lines, buffer = buffer.split(b"\n")
                    for line in lines:
                        line = line.strip()
                        if not line.startswith(b"data:") or line == b"data: [DONE]":
                            continue
                        try:
                            event = json.loads(line[5:])
                        except json.JSONDecodeError:
                            continue
                        used = event.get("model", used)
                        usage = _usage_of(event) or usage
            finally:
                await resp.aclose()
                await finish(grant, reservation, model, used, usage, started, resp.status_code)

        return StreamingResponse(
            stream(),
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "text/event-stream"),
        )

    return app
