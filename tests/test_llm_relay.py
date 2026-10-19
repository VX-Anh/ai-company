from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from aico.core import Ticket
from aico.db import create_pool
from aico.llm_relay import (
    BudgetExceeded,
    InMemoryBudgetLedger,
    PostgresBudgetLedger,
    RelayDeps,
    TaskTokens,
    create_relay_app,
)
from aico.observability import RecordingTraceSink
from aico.registry import Company
from aico.tickets.postgres import PostgresTicketStore

UPSTREAM_KEY = "nine-router-secret"


def _upstream(seen: list[httpx.Request], *, stream: bool = False) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        if body.get("stream"):
            events = [
                {"model": "provider/real-model", "choices": [{"delta": {"content": "Xin "}}]},
                {"model": "provider/real-model", "choices": [{"delta": {"content": "chào"}}]},
                {
                    "model": "provider/real-model",
                    "choices": [],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                },
            ]
            text = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
        return httpx.Response(
            200,
            json={
                "model": "provider/real-model",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
            },
        )

    return httpx.AsyncClient(
        base_url="http://9router/v1/",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": f"Bearer {UPSTREAM_KEY}"},
    )


def _relay(
    company: Company, ledger: Any, seen: list[httpx.Request]
) -> tuple[httpx.AsyncClient, TaskTokens, RecordingTraceSink]:
    tokens = TaskTokens()
    trace = RecordingTraceSink()
    app = create_relay_app(RelayDeps(company, tokens, ledger, _upstream(seen), trace))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://relay")
    return client, tokens, trace


def _chat(model: str = "coder-model", **kw: Any) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "sửa lỗi"}],
        "max_tokens": 100,
        **kw,
    }


async def test_requires_task_token_and_allowed_model(company: Company) -> None:
    seen: list[httpx.Request] = []
    client, tokens, _ = _relay(company, InMemoryBudgetLedger(), seen)
    assert (await client.post("/v1/chat/completions", json=_chat())).status_code == 401
    token = tokens.issue("T-1", "coder")
    auth = {"Authorization": f"Bearer {token}"}
    wrong = await client.post("/v1/chat/completions", json=_chat("planner-model"), headers=auth)
    assert wrong.status_code == 403
    models = await client.get("/v1/models", headers=auth)
    assert [m["id"] for m in models.json()["data"]] == ["coder-model"]
    tokens.revoke(token)
    assert (
        await client.post("/v1/chat/completions", json=_chat(), headers=auth)
    ).status_code == 401
    assert seen == []


async def test_forwards_with_upstream_key_and_settles_usage(company: Company) -> None:
    seen: list[httpx.Request] = []
    ledger = InMemoryBudgetLedger()
    client, tokens, trace = _relay(company, ledger, seen)
    token = tokens.issue("T-1", "coder")
    resp = await client.post(
        "/v1/chat/completions", json=_chat(), headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "ok"
    assert seen[0].headers["authorization"] == f"Bearer {UPSTREAM_KEY}"
    assert token not in seen[0].headers.get("authorization", "")
    usd, used_tokens = ledger.spent("T-1")
    assert used_tokens == 1500
    assert usd == pytest.approx((1000 * 3 + 500 * 15) / 1e6)
    assert trace.items[0].model_used == "provider/real-model"


async def test_streaming_passes_through_and_counts_usage(company: Company) -> None:
    seen: list[httpx.Request] = []
    ledger = InMemoryBudgetLedger()
    client, tokens, trace = _relay(company, ledger, seen)
    token = tokens.issue("T-1", "coder")
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json=_chat(stream=True),
        headers={"Authorization": f"Bearer {token}"},
    ) as resp:
        text = "".join([part async for part in resp.aiter_text()])
    assert "Xin " in text and "[DONE]" in text
    assert json.loads(seen[0].content)["stream_options"] == {"include_usage": True}
    assert ledger.spent("T-1")[1] == 120
    assert trace.items[0].completion_tokens == 20


async def test_budget_exceeded_returns_429(company: Company) -> None:
    seen: list[httpx.Request] = []
    ledger = InMemoryBudgetLedger(ticket_budgets={"T-1": 0.0001})
    client, tokens, _ = _relay(company, ledger, seen)
    token = tokens.issue("T-1", "coder")
    resp = await client.post(
        "/v1/chat/completions", json=_chat(), headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 429
    assert resp.json()["error"]["type"] == "budget_exceeded"
    assert seen == []


async def test_role_token_cap(company: Company) -> None:
    ledger = InMemoryBudgetLedger(default=1000)
    coder = company.role("coder")
    cap = coder.budget.per_ticket_tokens  # type: ignore[union-attr]
    assert cap is not None
    await ledger.reserve("T-1", coder, "coder-model", cap - 10, 0)
    with pytest.raises(BudgetExceeded, match="trần token"):
        await ledger.reserve("T-1", coder, "coder-model", 20, 0)


async def test_postgres_ledger_serializes_concurrent_reservations(
    company: Company, pg_clean: str
) -> None:
    pool = create_pool(pg_clean)
    await pool.open()
    try:
        tickets = PostgresTicketStore(pool)
        await tickets.create(Ticket(id="T-1", title="x", budget_usd=1.0), actor="test")
        ledger = PostgresBudgetLedger(pool)
        coder = company.role("coder")
        results = await asyncio.gather(
            *(ledger.reserve("T-1", coder, "coder-model", 1000, 0.3) for _ in range(6)),
            return_exceptions=True,
        )
        ok = [r for r in results if not isinstance(r, BaseException)]
        assert len(ok) == 3  # 3 x 0.3 <= 1.0, cái thứ 4 vượt
        assert all(isinstance(r, BudgetExceeded) for r in results if isinstance(r, BaseException))
        await ledger.settle(ok[0], 500, 0.1)
        await ledger.cancel(ok[1])
        t = await tickets.get("T-1")
        assert t.spent_usd == pytest.approx(0.1) and t.spent_tokens == 500
        # Đã nhả 0.2 (settle) + 0.3 (cancel): còn chỗ cho một khoản 0.3 nữa.
        await ledger.reserve("T-1", coder, "coder-model", 1000, 0.3)
    finally:
        await pool.close()
