"""Token tạm theo task: gắn ticket và vai trò, chỉ lưu bản băm, hết hạn khi task xong."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from aico.core import utcnow


@dataclass(frozen=True)
class TaskGrant:
    ticket_id: str
    role: str
    expires_at: datetime


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TaskTokens:
    def __init__(self) -> None:
        self._grants: dict[str, TaskGrant] = {}

    def issue(self, ticket_id: str, role: str, ttl: timedelta = timedelta(hours=1)) -> str:
        token = "aico_" + secrets.token_urlsafe(32)
        self._grants[_digest(token)] = TaskGrant(ticket_id, role, utcnow() + ttl)
        return token

    def resolve(self, token: str) -> TaskGrant | None:
        grant = self._grants.get(_digest(token))
        if grant is None:
            return None
        if grant.expires_at <= utcnow():
            self._grants.pop(_digest(token), None)
            return None
        return grant

    def revoke(self, token: str) -> None:
        self._grants.pop(_digest(token), None)
