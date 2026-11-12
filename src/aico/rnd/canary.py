"""Canary (Phòng R&D, R2 mục 2.4): N ticket đầu chạy bằng cấu hình mới so với N ticket ngay trước.

Mỗi event `job` mang `company_sha` (mốc deploy + dấu vân tay company/, xem agents.cli_agent).
Ticket thuộc cấu hình nào tính theo job ĐẦU TIÊN của nó: ticket đang chạy dở lúc deploy vẫn thuộc
nhóm cũ. Module chỉ báo cáo: không revert, không tạo ticket; người đọc kết quả tự quyết.

Đây là quan sát trước–sau trên mẫu nhỏ, không phải thí nghiệm đối chứng: ticket hai nhóm khác đề
bài, nên cờ `regressed` là tín hiệu để xem lại, không phải kết luận.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from aico.rnd.bench import EXCLUDED_LABELS
from aico.rnd.metrics import aggregate, collect
from aico.tickets import TicketStore

FINISHED = ("done", "failed", "escalated")
# Ngưỡng mặc định (thiết kế R2, câu hỏi 2 cho owner): tỷ lệ đạt vòng đầu giảm từ 15 điểm phần
# trăm, hoặc số vòng review trung bình tăng từ 0,5.
FIRST_PASS_DROP_POINTS = 15.0
REVIEW_ROUNDS_INCREASE = 0.5


def sha_matches(company_sha: object, wanted: str) -> bool:
    """`wanted` khớp cả chuỗi, hoặc là đoạn đầu (từ 7 ký tự) của mốc deploy hay dấu vân tay."""
    if not isinstance(company_sha, str) or not wanted:
        return False
    if company_sha == wanted:
        return True
    return len(wanted) >= 7 and any(part.startswith(wanted) for part in company_sha.split("-"))


def _tokens(row: Mapping[str, Any]) -> int:
    return sum(int(j.get("tokens_in") or 0) + int(j.get("tokens_out") or 0) for j in row["jobs"])


def _group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    agg = aggregate(rows)
    return {
        "tickets": len(rows),
        "done": agg["done"],
        "escalated": agg["escalated"],
        "failed": agg["failed"],
        "first_pass_rate": agg["first_pass_rate"],
        "avg_review_rounds": agg["avg_review_rounds"],
        "owner_return_rate": agg["owner_return_rate"],
        "avg_tokens": round(sum(_tokens(r) for r in rows) / len(rows)) if rows else None,
        "ids": [r["id"] for r in rows],
        "company_shas": dict(Counter(str(r["company_sha"] or "?") for r in rows).most_common()),
    }


def canary(
    rows: list[dict[str, Any]],
    *,
    sha: str | None = None,
    n: int = 10,
    first_pass_drop: float = FIRST_PASS_DROP_POINTS,
    review_increase: float = REVIEW_ROUNDS_INCREASE,
    code_only: bool = True,
) -> dict[str, Any]:
    """So n ticket đầu của cấu hình `sha` với n ticket kết thúc ngay trước đó (hàm thuần).

    rows: kết quả metrics.ticket_metrics. Thứ tự tính theo lúc job đầu tiên chạy; ticket chưa có
    job bị bỏ. sha=None: lấy cấu hình của ticket bắt đầu gần nhất. Chỉ ticket đã kết thúc mới vào
    hai nhóm. `regressed` là None khi nhóm sau chưa đủ n ticket hoặc nhóm trước rỗng.
    """
    started = sorted(
        (
            r
            for r in rows
            if r.get("started_at")
            and not (code_only and EXCLUDED_LABELS & set(r.get("labels") or ()))
        ),
        key=lambda r: datetime.fromisoformat(r["started_at"]),
    )
    result: dict[str, Any] = {
        "sha": sha,
        "n": n,
        "code_only": code_only,
        "thresholds": {"first_pass_drop": first_pass_drop, "review_increase": review_increase},
        "note": "Quan sát trước–sau trên mẫu nhỏ, đề bài hai nhóm khác nhau: tín hiệu để xem "
        "lại, không phải kết luận. Không tự revert.",
    }
    if sha is None:
        sha = next((r["company_sha"] for r in reversed(started) if r.get("company_sha")), None)
        result["sha"] = sha
    last = next(
        (i for i in range(len(started) - 1, -1, -1) if sha_matches(started[i]["company_sha"], sha))
        if sha
        else iter(()),
        None,
    )
    if sha is None or last is None:
        reason = (
            "chưa event job nào có company_sha" if sha is None else f"không ticket nào chạy {sha}"
        )
        return {
            **result,
            "after": _group([]),
            "before": _group([]),
            "complete": False,
            "regressed": None,
            "reasons": [reason],
        }
    # Đợt chạy gần nhất của cấu hình này: lùi từ ticket khớp cuối cùng tới khi gặp cấu hình khác
    # (cùng nội dung company/ có thể quay lại sau một lần revert, không gộp hai đợt).
    first = last
    while first > 0 and sha_matches(started[first - 1]["company_sha"], sha):
        first -= 1
    after = [r for r in started[first : last + 1] if r["status"] in FINISHED][:n]
    before = [r for r in started[:first] if r["status"] in FINISHED][-n:]
    a, b = _group(after), _group(before)
    reasons: list[str] = []
    drop = increase = None
    if a["first_pass_rate"] is not None and b["first_pass_rate"] is not None:
        drop = round((b["first_pass_rate"] - a["first_pass_rate"]) * 100, 1)
        if drop >= first_pass_drop:
            reasons.append(f"tỷ lệ đạt vòng đầu giảm {drop} điểm (ngưỡng {first_pass_drop})")
    if a["avg_review_rounds"] is not None and b["avg_review_rounds"] is not None:
        increase = round(a["avg_review_rounds"] - b["avg_review_rounds"], 2)
        if increase >= review_increase:
            reasons.append(f"vòng review TB tăng {increase} (ngưỡng {review_increase})")
    complete = len(after) >= n
    regressed: bool | None = bool(reasons)
    if not complete or not before:
        regressed = None
        reasons.append(
            f"chưa đủ mẫu: nhóm sau {len(after)}/{n} ticket đã kết thúc, nhóm trước {len(before)}"
        )
    return {
        **result,
        "after": a,
        "before": b,
        "first_pass_drop": drop,
        "review_increase": increase,
        "complete": complete,
        "regressed": regressed,
        "reasons": reasons,
    }


async def canary_report(
    store: TicketStore,
    *,
    now: datetime,
    sha: str | None = None,
    n: int = 10,
    days: int = 90,
    first_pass_drop: float = FIRST_PASS_DROP_POINTS,
    review_increase: float = REVIEW_ROUNDS_INCREASE,
    code_only: bool = True,
) -> dict[str, Any]:
    """Canary trên ticket thật (không tính ticket chạy lại nhãn eval) tạo trong `days` ngày."""
    rows = await collect(store, since=now - timedelta(days=days))
    return {
        "days": days,
        "generated_at": now.isoformat(),
        **canary(
            rows,
            sha=sha,
            n=n,
            first_pass_drop=first_pass_drop,
            review_increase=review_increase,
            code_only=code_only,
        ),
    }
