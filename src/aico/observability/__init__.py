"""Trace model và workflow. Langfuse cắm vào sau khi chốt Langfuse chạy ở đâu."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("aico.trace")


@dataclass(frozen=True)
class Generation:
    ticket_id: str
    role: str
    model_requested: str
    model_used: str | None
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: float
    status: int


class TraceSink(Protocol):
    async def generation(self, g: Generation) -> None: ...


class LoggingTraceSink:
    async def generation(self, g: Generation) -> None:
        log.info(
            "llm ticket=%s role=%s model=%s->%s tokens=%d+%d usd=%.5f %.0fms status=%d",
            g.ticket_id,
            g.role,
            g.model_requested,
            g.model_used,
            g.prompt_tokens,
            g.completion_tokens,
            g.cost_usd,
            g.latency_ms,
            g.status,
        )


class RecordingTraceSink:
    def __init__(self) -> None:
        self.items: list[Generation] = []

    async def generation(self, g: Generation) -> None:
        self.items.append(g)
