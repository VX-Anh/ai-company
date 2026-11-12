"""Chạy lại bộ ticket chuẩn với một biến thể cấu hình (Phòng R&D, R1).

`aico eval start`: tạo ticket nhãn `eval` từ bộ chuẩn (JSON, định dạng của GET /rnd/bench), mỗi
ticket gắn artifact `eval` = "<biến thể>@<commit gốc>". Instance `aico-eval` (AICO_EVAL_INSTANCE=1,
AICO_COMPANY_DIR trỏ tới thư mục biến thể) chạy chúng trên graph thật: nhánh ai/<id> từ commit gốc,
MR vào base/<id>, không merge. Tiêu đề, tên nhánh và prompt không lộ việc đang chạy lại.

`aico eval report`: gộp kết quả theo biến thể, so với số liệu lúc chạy thật (`observed`) nếu có;
`--close` từ chối ticket đang chờ duyệt merge để đóng lượt chạy (ticket thành escalated).
"""

from __future__ import annotations

import re
from collections import defaultdict
from statistics import mean
from typing import Any

from aico.core import Artifact, Ticket, TicketStatus
from aico.intake import new_ticket_id
from aico.orchestrator.workplan import EVAL_LABEL, eval_spec
from aico.rnd.metrics import ticket_metrics
from aico.tickets import TicketStore

S = TicketStatus
_VARIANT = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
# Ticket xong lượt chạy lại: tới chờ duyệt merge (review + test đạt) hoặc dừng.
FINISHED = frozenset({S.AWAITING_MERGE_APPROVAL, S.ESCALATED, S.FAILED, S.DONE})


class EvalError(ValueError):
    pass


def eval_tickets(suite: list[dict[str, Any]], variant: str, repeat: int = 1) -> list[Ticket]:
    """Ticket chạy lại cho mỗi mục của bộ chuẩn × số lần lặp (agent không tất định)."""
    if not _VARIANT.fullmatch(variant):
        raise EvalError("tên biến thể: chữ thường, số, '-' (tối đa 41 ký tự)")
    if repeat < 1:
        raise EvalError("repeat phải từ 1")
    out: list[Ticket] = []
    for item in suite:
        if "skipped" in item:
            continue
        missing = [k for k in ("id", "project", "title", "base_sha") if not item.get(k)]
        if missing:
            raise EvalError(f"mục {item.get('id', '?')} thiếu {', '.join(missing)}")
        base = str(item["base_sha"])
        if re.fullmatch(r"[0-9a-f]{7,39}", base):
            raise EvalError(
                f"mục {item['id']}: base_sha phải đủ 40 ký tự (SHA ngắn bị coi là tên nhánh)"
            )
        labels = [x for x in item.get("labels") or [] if str(x).lower() != EVAL_LABEL]
        for n in range(1, repeat + 1):
            out.append(
                Ticket(
                    id=new_ticket_id(),
                    # Tiêu đề giữ nguyên: agent không biết đây là lượt chạy lại. Biến thể nằm ở
                    # artifact eval và source_ref.
                    title=str(item["title"]),
                    body=str(item.get("body") or ""),
                    project=str(item["project"]),
                    labels=[*labels, EVAL_LABEL],
                    # Khoá nguồn: chạy lại cùng lệnh không tạo trùng (create_from_source).
                    source_ref=f"eval:{variant}:{item['id']}:{n}",
                    artifacts=[Artifact(kind="eval", ref=f"{variant}@{item['base_sha']}")],
                )
            )
    return out


async def start(
    store: TicketStore, suite: list[dict[str, Any]], variant: str, repeat: int = 1
) -> list[tuple[str, bool]]:
    """Tạo ticket chạy lại; trả (id, mới tạo?) — đã có thì giữ nguyên."""
    created: list[tuple[str, bool]] = []
    for ticket in eval_tickets(suite, variant, repeat):
        got, new = await store.create_from_source(ticket, actor="eval")
        created.append((got.id, new))
    return created


def _origin(ticket: Ticket) -> str:
    parts = (ticket.source_ref or "").split(":")
    return parts[2] if len(parts) >= 4 else ticket.id


async def report(
    store: TicketStore,
    variant: str,
    *,
    observed: dict[str, dict[str, Any]] | None = None,
    close: bool = False,
) -> dict[str, Any]:
    """Kết quả của biến thể: mỗi ticket gốc một dòng (gộp các lần lặp) và tổng."""
    rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    running = 0
    for ticket in await store.list():
        spec = eval_spec(ticket)
        if spec is None or spec[0] != variant:
            continue
        if ticket.status not in FINISHED:
            running += 1
            continue
        m = ticket_metrics(ticket, await store.events(ticket.id))
        tokens = sum(
            int(j.get("tokens_in") or 0) + int(j.get("tokens_out") or 0) for j in m["jobs"]
        )
        rows[_origin(ticket)].append(
            {
                "ticket": ticket.id,
                # Đạt = đã tới chờ duyệt merge (review và test đạt trong giới hạn vòng), dù sau
                # đó bị đóng (--close), quá hạn duyệt hay từ chối.
                "passed": m["minutes_to_merge_approval"] is not None,
                "code_rounds": m["code_rounds"],
                "review_rounds": m["review_rounds"],
                "tokens": tokens,
                "minutes": m["minutes_to_merge_approval"],
                "escalation": m["escalation_class"],
            }
        )
        if close and ticket.status is S.AWAITING_MERGE_APPROVAL:
            await store.set_pending_resume(
                ticket.id,
                {"decision": "rejected", "actor": "eval", "comment": "lượt chạy lại đã chấm"},
                actor="eval",
            )
    per_origin = []
    for origin, runs in sorted(rows.items()):
        line: dict[str, Any] = {
            "origin": origin,
            "runs": len(runs),
            "pass_rate": round(sum(r["passed"] for r in runs) / len(runs), 3),
            "first_pass_rate": round(
                sum(r["passed"] and r["code_rounds"] <= 1 for r in runs) / len(runs), 3
            ),
            "avg_review_rounds": round(mean(r["review_rounds"] for r in runs), 2),
            "avg_tokens": round(mean(r["tokens"] for r in runs)),
        }
        if observed and origin in observed:
            line["observed"] = observed[origin]
        per_origin.append(line)
    all_runs = [r for runs in rows.values() for r in runs]
    total = (
        {
            "runs": len(all_runs),
            "pass_rate": round(sum(r["passed"] for r in all_runs) / len(all_runs), 3),
            "first_pass_rate": round(
                sum(r["code_rounds"] <= 1 for r in all_runs) / len(all_runs), 3
            ),
            "avg_review_rounds": round(mean(r["review_rounds"] for r in all_runs), 2),
            "avg_tokens": round(mean(r["tokens"] for r in all_runs)),
        }
        if all_runs
        else {"runs": 0}
    )
    return {"variant": variant, "running": running, "total": total, "tickets": per_origin}
