"""Chạy lại bộ ticket chuẩn với một biến thể cấu hình (Phòng R&D, R1).

`aico eval start`: tạo ticket nhãn `eval` từ bộ chuẩn (JSON, định dạng của GET /rnd/bench), mỗi
ticket gắn artifact `eval` = "<biến thể>@<commit gốc>". Instance `aico-eval` (AICO_EVAL_INSTANCE=1,
AICO_COMPANY_DIR trỏ tới thư mục biến thể) chạy chúng trên graph thật: nhánh ai/<id> từ commit gốc,
MR vào base/<id>, không merge. Tiêu đề, tên nhánh và prompt không lộ việc đang chạy lại.

`aico eval report`: gộp kết quả theo biến thể, so với số liệu lúc chạy thật (`observed`) nếu có;
`--close` từ chối ticket đang chờ duyệt merge để đóng lượt chạy (ticket thành escalated).

`aico eval grade`: chấm acceptance của ticket chạy lại đã tới chờ duyệt merge — chạy lệnh
`acceptance: {build, test}` của mục bộ chuẩn trên máy build với mã nguồn nhánh ai/<id>, ghi event
`eval_acceptance`. Chấm sau khi agent xong; agent không thấy lệnh hay kết quả.

`aico eval cleanup`: xoá cặp nhánh ai/<id> + base/<id> của ticket chạy lại đã đóng (tool
gitlab.delete_branch_pair qua guard + audit, vai trò hệ thống `housekeeper`).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from statistics import mean
from typing import Any

from aico.core import Artifact, Ticket, TicketStatus
from aico.intake import new_ticket_id
from aico.orchestrator.workplan import EVAL_LABEL, eval_spec, ticket_refs
from aico.rnd.metrics import ticket_metrics
from aico.tickets import TicketStore
from aico.tools import ToolGateway

S = TicketStatus
_VARIANT = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
# Ticket xong lượt chạy lại: tới chờ duyệt merge (review + test đạt) hoặc dừng.
FINISHED = frozenset({S.AWAITING_MERGE_APPROVAL, S.ESCALATED, S.FAILED, S.DONE})
# Ticket đã đóng hẳn (không còn bước nào dùng nhánh): mới được dọn nhánh.
CLOSED = frozenset({S.ESCALATED, S.FAILED, S.DONE})
CLEANUP_EVENT = "eval_cleanup"
CLEANUP_ROLE = "housekeeper"
# (ticket_id, project, nhánh ai/<id>) -> kết quả tool delete_branch_pair.
BranchDeleter = Callable[[str, str, str], Awaitable[dict[str, Any]]]


ACCEPTANCE_EVENT = "eval_acceptance"
ACCEPTANCE_STEPS = ("build", "test")  # thứ tự chạy; build hỏng thì không chạy test
OUTPUT_TAIL_CHARS = 2000
# (ticket_id, project, ref, {bước: lệnh}, image) -> {bước: {exit_code, output, ...}}; lỗi hạ tầng
# thì raise (không tính là hỏng acceptance).
AcceptanceRunner = Callable[
    [str, str, str, Mapping[str, str], str | None], Awaitable[dict[str, dict[str, Any]]]
]


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


def _acceptance_stats(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Chỉ số acceptance của một nhóm lượt chạy; không lượt nào được chấm thì không có trường nào.

    - `acceptance_pass_rate`: đạt acceptance / số lượt đã chấm (chỉ lượt tới chờ duyệt merge mới
      được chấm; lượt dừng trước đó đã tính ở `pass_rate`).
    - `review_pass_acceptance_fail_rate`: chỉ số chặn — trong các lượt Reviewer/Tester cho đạt và
      đã chấm, bao nhiêu phần hỏng acceptance (Reviewer dễ dãi). Xấu đi là loại biến thể.
    - `acceptance_ungraded`: lượt đạt review nhưng chưa chấm (số trên chưa phủ hết).
    """
    graded = [r for r in runs if r["acceptance"] is not None]
    if not graded:
        return {}
    reviewed = [r for r in graded if r["passed"]]
    return {
        "acceptance_graded": len(graded),
        "acceptance_pass_rate": round(sum(r["acceptance"] for r in graded) / len(graded), 3),
        "review_pass_acceptance_fail_rate": (
            round(sum(not r["acceptance"] for r in reviewed) / len(reviewed), 3)
            if reviewed
            else None
        ),
        "acceptance_ungraded": sum(1 for r in runs if r["passed"] and r["acceptance"] is None),
    }


def acceptance_of(item: Mapping[str, Any]) -> dict[str, str]:
    """Lệnh chấm của một mục bộ chuẩn theo thứ tự chạy ({} nếu mục không khai báo)."""
    spec = item.get("acceptance")
    if spec is None:
        return {}
    if not isinstance(spec, Mapping):
        raise EvalError(f"mục {item.get('id', '?')}: acceptance phải là {{build, test}}")
    unknown = set(spec) - {*ACCEPTANCE_STEPS, "image"}
    if unknown:
        raise EvalError(f"mục {item.get('id', '?')}: acceptance có khoá lạ {sorted(unknown)}")
    return {k: str(spec[k]).strip() for k in ACCEPTANCE_STEPS if str(spec.get(k) or "").strip()}


async def grade(
    store: TicketStore,
    variant: str,
    suite: list[dict[str, Any]],
    run: AcceptanceRunner,
    *,
    default_project: str | None = None,
    regrade: bool = False,
) -> list[dict[str, Any]]:
    """Chấm acceptance cho ticket chạy lại của biến thể đã tới chờ duyệt merge; mỗi ticket một dòng.

    Chỉ chấm lượt Reviewer/Tester đã cho đạt (đang chờ duyệt merge, hoặc đã đóng mà chưa dọn
    nhánh): đó là nơi cần biết review có dễ dãi không. Lệnh lấy từ bộ chuẩn (owner soạn), chạy trong
    container dùng một lần không mạng của máy build. Kết quả ghi event `eval_acceptance`; lỗi hạ
    tầng không ghi (không tính là hỏng), chạy lại lệnh sẽ chấm tiếp.
    """
    specs = {str(i["id"]): i for i in suite if i.get("id") and "skipped" not in i}
    commands = {origin: acceptance_of(item) for origin, item in specs.items()}
    out: list[dict[str, Any]] = []
    for ticket in await store.list():
        spec = eval_spec(ticket)
        if spec is None or spec[0] != variant:
            continue
        origin = _origin(ticket)
        if not commands.get(origin):
            continue  # mục không khai báo acceptance (hoặc không còn trong bộ chuẩn)
        events = await store.events(ticket.id)
        reached = any(e.to_status is S.AWAITING_MERGE_APPROVAL for e in events)
        if not reached or ticket.status not in (S.AWAITING_MERGE_APPROVAL, *CLOSED):
            continue
        row: dict[str, Any] = {"ticket": ticket.id, "origin": origin}
        out.append(row)
        project = ticket.project or default_project
        if any(e.kind == CLEANUP_EVENT for e in events):
            row["skipped"] = "đã dọn nhánh"
        elif not regrade and any(e.kind == ACCEPTANCE_EVENT for e in events):
            row["skipped"] = "đã chấm"
        elif not project:
            row["skipped"] = "ticket không có repo"
        else:
            image = (specs[origin].get("acceptance") or {}).get("image")
            ref = ticket_refs(ticket).work
            try:
                steps = await run(
                    ticket.id, project, ref, commands[origin], str(image) if image else None
                )
            except Exception as exc:  # máy build lỗi: không phải lỗi của bài làm
                row["error"] = str(exc)
                continue
            summary = {
                name: {
                    "exit_code": int(result.get("exit_code", -1)),
                    "duration_seconds": result.get("duration_seconds"),
                    "output_tail": str(result.get("output") or "")[-OUTPUT_TAIL_CHARS:],
                }
                for name, result in steps.items()
            }
            # Đạt = mọi bước đã khai báo đều chạy và exit 0 (bước bị bỏ vì bước trước hỏng = hỏng).
            passed = all(
                name in summary and summary[name]["exit_code"] == 0 for name in commands[origin]
            )
            row["passed"] = passed
            row["steps"] = {name: s["exit_code"] for name, s in summary.items()}
            await store.record(
                ticket.id,
                ACCEPTANCE_EVENT,
                actor="eval",
                data={"passed": passed, "ref": ref, "steps": summary},
            )
    return out


async def report(
    store: TicketStore,
    variant: str,
    *,
    observed: dict[str, dict[str, Any]] | None = None,
    close: bool = False,
    need_grade: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Kết quả của biến thể: mỗi ticket gốc một dòng (gộp các lần lặp) và tổng.

    `need_grade`: id ticket gốc có `acceptance` trong bộ chuẩn; `close` không đóng lượt chạy của
    chúng khi chưa chấm (đếm ở `close_skipped_ungraded`).
    """
    rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    running = ungraded_open = 0
    for ticket in await store.list():
        spec = eval_spec(ticket)
        if spec is None or spec[0] != variant:
            continue
        if ticket.status not in FINISHED:
            running += 1
            continue
        events = await store.events(ticket.id)
        m = ticket_metrics(ticket, events)
        graded = [ev.data for ev in events if ev.kind == ACCEPTANCE_EVENT]
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
                # True/False: kết quả chấm acceptance gần nhất; None: chưa chấm.
                "acceptance": bool(graded[-1].get("passed")) if graded else None,
            }
        )
        if close and ticket.status is S.AWAITING_MERGE_APPROVAL:
            if not graded and _origin(ticket) in (need_grade or ()):
                # Đóng rồi dọn nhánh thì không còn gì để chấm: chấm trước (aico eval grade).
                ungraded_open += 1
                continue
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
            **_acceptance_stats(runs),
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
            **_acceptance_stats(all_runs),
        }
        if all_runs
        else {"runs": 0}
    )
    out: dict[str, Any] = {"variant": variant, "running": running}
    if ungraded_open:
        out["close_skipped_ungraded"] = ungraded_open
    return {**out, "total": total, "tickets": per_origin}


def gateway_deleter(gateway: ToolGateway) -> BranchDeleter:
    """Xoá cặp nhánh qua gateway (guard + audit) bằng vai trò hệ thống `housekeeper`."""

    async def delete(ticket_id: str, project: str, branch: str) -> dict[str, Any]:
        return await gateway.call(
            CLEANUP_ROLE,
            "gitlab",
            "delete_branch_pair",
            {"project": project, "branch": branch},
            ticket_id=ticket_id,
        )

    return delete


async def cleanup(
    store: TicketStore,
    variant: str,
    delete: BranchDeleter,
    *,
    default_project: str | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Dọn nhánh của các ticket chạy lại đã đóng của biến thể; mỗi ticket một dòng kết quả.

    Chỉ ticket có nhãn + artifact eval (cùng vị từ với claim) và đã đóng hẳn: ticket đang chạy
    hay còn chờ chấm vẫn cần nhánh. Tên nhánh lấy từ `ticket_refs`, không nhận từ người gọi.
    Dọn xong ghi event `eval_cleanup` để lần sau bỏ qua; lỗi thì không ghi, chạy lại sẽ thử tiếp.
    """
    out: list[dict[str, Any]] = []
    for ticket in await store.list():
        spec = eval_spec(ticket)
        if spec is None or spec[0] != variant:
            continue
        row: dict[str, Any] = {"ticket": ticket.id}
        out.append(row)
        refs = ticket_refs(ticket)
        project = ticket.project or default_project
        if ticket.status not in CLOSED or ticket.pending_resume:
            row["skipped"] = f"chưa đóng ({ticket.status.value})"
        elif any(e.kind == CLEANUP_EVENT for e in await store.events(ticket.id)):
            row["skipped"] = "đã dọn"
        elif not project:
            row["skipped"] = "ticket không có repo"
        elif dry_run:
            row["would_delete"] = [refs.work, refs.target]
        else:
            try:
                result = await delete(ticket.id, project, refs.work)
            except Exception as exc:  # một ticket lỗi không chặn các ticket còn lại
                row["error"] = str(exc)
                continue
            row["deleted"] = list(result.get("deleted") or [])
            await store.record(
                ticket.id,
                CLEANUP_EVENT,
                actor="eval",
                data={"deleted": row["deleted"], "heads": dict(result.get("heads") or {})},
            )
    return out
