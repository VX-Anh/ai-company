"""Canary (Phòng R&D, R2 mục 2.4): N ticket đầu chạy bằng cấu hình mới so với N ticket ngay trước.

Mỗi event `job` mang `company_sha` (mốc deploy + dấu vân tay company/, xem agents.cli_agent).
Ticket thuộc một cấu hình khi MỌI job của nó chạy bằng cấu hình đó; ticket vắt qua một lần deploy
(job mang hơn một company_sha) không thuộc trọn nhóm nào nên bị loại khỏi cả hai nhóm và được đếm
riêng. Module chỉ báo cáo: không revert, không tạo ticket; người đọc kết quả tự quyết.

Hai nhóm chọn theo lúc BẮT ĐẦU (job đầu tiên), không theo lúc kết thúc: nhóm sau là n ticket đầu
của đợt, nhóm trước là n ticket bắt đầu ngay trước đợt. Chọn theo "đã kết thúc" sẽ để ticket dễ
(xong nhanh) lấp chỗ ticket kẹt hay còn chờ duyệt, làm cấu hình mới trông tốt hơn thật.

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
# trăm, số vòng review trung bình tăng từ 0,5, hoặc tỷ lệ kết thúc không-done (escalated + failed
# trên số ticket đã kết thúc) tăng từ 15 điểm.
FIRST_PASS_DROP_POINTS = 15.0
REVIEW_ROUNDS_INCREASE = 0.5
NON_DONE_INCREASE_POINTS = 15.0
# Đoạn sha ngắn nhất được coi là khớp (ngắn hơn dễ khớp nhầm), và ký tự `aico-ops deploy` chèn
# giữa git sha và băm phần chưa commit ("x" không phải chữ số hex).
MIN_SHA_CHARS = 7
DIRTY_MARK = "x"


def _split_mark(mark: str) -> tuple[str, str | None]:
    """Mốc deploy -> (git sha, băm phần chưa commit hoặc None khi bản nguồn sạch)."""
    git, sep, dirty = mark.partition(DIRTY_MARK)
    return git, dirty if sep else None


def _deploy_matches(mark: str, wanted: str) -> bool:
    git, dirty = _split_mark(mark)
    want_git, want_dirty = _split_mark(wanted)
    # Sạch chỉ khớp sạch, bẩn chỉ khớp bẩn: bản deploy có sửa chưa commit là cấu hình khác.
    if dirty is None or want_dirty is None:
        if dirty is not None or want_dirty is not None:
            return False
    elif not dirty.startswith(want_dirty):
        return False
    # Mốc lưu 11 ký tự, người dùng có thể dán 7 hay đủ 40: bên ngắn phải là đoạn đầu bên dài.
    if min(len(git), len(want_git)) < MIN_SHA_CHARS:
        return False
    return git.startswith(want_git) or want_git.startswith(git)


def sha_matches(company_sha: object, wanted: object) -> bool:
    """`wanted` (người dùng gõ) có chỉ cấu hình `company_sha` ("<mốc deploy>-<dấu vân tay>") không.

    Không phân biệt hoa thường. Quy tắc, theo thứ tự:
    - Khớp cả chuỗi. `wanted` có dấu "-" (dán cả mốc) thì chỉ khớp cả chuỗi.
    - Dấu vân tay company/: `wanted` là đoạn đầu, từ 7 ký tự.
    - Mốc deploy sạch "<git sha>": so với `wanted` không có "x"; bên ngắn hơn là đoạn đầu của
      bên dài hơn và đoạn chung từ 7 ký tự (dán sha 40 ký tự vẫn khớp mốc 11 ký tự).
    - Mốc deploy bẩn "<git sha>x<băm>": chỉ khớp `wanted` cũng có "x": phần git sha như trên,
      phần băm là đoạn đầu (để trống sau "x" = mọi bản bẩn của commit đó). Sha sạch KHÔNG khớp
      bản bẩn, và ngược lại.
    Mốc kiểu cũ "<git sha>d" (trước khi có băm) không tách được nên coi như một sha sạch 12 ký tự.
    """
    if not isinstance(company_sha, str) or not isinstance(wanted, str):
        return False
    have, want = company_sha.strip().lower(), wanted.strip().lower()
    if not have or not want:
        return False
    if have == want:
        return True
    if "-" in want:
        return False
    parts = have.split("-")
    if len(want) >= MIN_SHA_CHARS and parts[-1].startswith(want):
        return True
    # Chỉ một phần: không biết là dấu vân tay hay mốc deploy (thiếu một trong hai), thử cả hai.
    return _deploy_matches(parts[0], want)


def _tokens(row: Mapping[str, Any]) -> int:
    return sum(int(j.get("tokens_in") or 0) + int(j.get("tokens_out") or 0) for j in row["jobs"])


def _is_mixed(row: Mapping[str, Any]) -> bool:
    """Ticket vắt qua một lần deploy: job của nó mang hơn một company_sha (kể cả None lẫn sha)."""
    return len(set(row.get("company_shas") or ())) > 1


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


def _group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Số liệu một nhóm. Tỷ lệ chỉ tính trên ticket đã kết thúc; ticket còn chạy/chờ ở `pending`."""
    agg = aggregate(rows)
    return {
        "tickets": len(rows),
        "finished": agg["finished"],
        "done": agg["done"],
        "escalated": agg["escalated"],
        "failed": agg["failed"],
        "non_done_rate": _ratio(agg["escalated"] + agg["failed"], agg["finished"]),
        "first_pass_rate": agg["first_pass_rate"],
        "avg_review_rounds": agg["avg_review_rounds"],
        "owner_return_rate": agg["owner_return_rate"],
        "avg_tokens": round(sum(_tokens(r) for r in rows) / len(rows)) if rows else None,
        "ids": [r["id"] for r in rows],
        "pending": [r["id"] for r in rows if r["status"] not in FINISHED],
        "company_shas": dict(Counter(str(r["company_sha"] or "?") for r in rows).most_common()),
    }


def _latest_sha(rows: list[dict[str, Any]]) -> str | None:
    """Cấu hình đang chạy: company_sha của job mới nhất (kể cả của ticket vắt qua deploy)."""
    newest_first = sorted(
        rows,
        key=lambda r: datetime.fromisoformat(r.get("last_job_at") or r["started_at"]),
        reverse=True,
    )
    for r in newest_first:
        sha = r.get("last_company_sha", r.get("company_sha"))
        if sha:
            return str(sha)
    return None


def canary(
    rows: list[dict[str, Any]],
    *,
    sha: str | None = None,
    n: int = 10,
    first_pass_drop: float = FIRST_PASS_DROP_POINTS,
    review_increase: float = REVIEW_ROUNDS_INCREASE,
    non_done_increase: float = NON_DONE_INCREASE_POINTS,
    code_only: bool = True,
) -> dict[str, Any]:
    """So n ticket ĐẦU của cấu hình `sha` với n ticket bắt đầu ngay trước đó (hàm thuần).

    rows: kết quả metrics.ticket_metrics. Thứ tự tính theo lúc job đầu tiên chạy; ticket chưa có
    job bị bỏ; ticket vắt qua deploy bị loại khỏi cả hai nhóm (đếm ở `mixed`). sha=None: lấy cấu
    hình của job mới nhất.

    `complete` chỉ khi cả n ticket đầu của đợt đã kết thúc (ticket còn chạy hay chờ duyệt nằm ở
    `after.pending`, không bị ticket xong sau thế chỗ). `regressed`:
    - True: tỷ lệ đạt vòng đầu giảm, vòng review TB tăng, tỷ lệ kết thúc không-done tăng quá
      ngưỡng, hoặc nhóm sau không ticket nào done trong khi nhóm trước có.
    - None: chưa đủ mẫu, nhóm trước không có ticket đã kết thúc, hoặc có tiêu chí thiếu dữ liệu
      để so (một nhóm không có ticket done / không có lần review) mà các tiêu chí còn lại không
      báo xấu. Thiếu dữ liệu không bao giờ thành False.
    - False: mọi tiêu chí đều so được và không cái nào vượt ngưỡng.
    """
    with_jobs = [r for r in rows if r.get("started_at")]
    eligible = sorted(
        (r for r in with_jobs if not (code_only and EXCLUDED_LABELS & set(r.get("labels") or ()))),
        key=lambda r: datetime.fromisoformat(r["started_at"]),
    )
    started = [r for r in eligible if not _is_mixed(r)]
    result: dict[str, Any] = {
        "sha": sha,
        "n": n,
        "code_only": code_only,
        "thresholds": {
            "first_pass_drop": first_pass_drop,
            "review_increase": review_increase,
            "non_done_increase": non_done_increase,
        },
        "note": "Quan sát trước–sau trên mẫu nhỏ, đề bài hai nhóm khác nhau: tín hiệu để xem "
        "lại, không phải kết luận. Không tự revert.",
    }
    if sha is None:
        sha = _latest_sha(with_jobs)
        result["sha"] = sha
    last = next(
        (i for i in range(len(started) - 1, -1, -1) if sha_matches(started[i]["company_sha"], sha))
        if sha
        else iter(()),
        None,
    )
    if sha is None or last is None:
        # Ticket vắt qua deploy có chạm cấu hình này: lý do thường gặp khi đợt mới chưa có ticket.
        touching = [
            r["id"]
            for r in eligible
            if sha and _is_mixed(r) and any(sha_matches(s, sha) for s in r["company_shas"])
        ]
        reason = (
            "chưa event job nào có company_sha"
            if sha is None
            else f"không ticket nào chạy trọn bằng {sha}"
        )
        if touching:
            reason += f" ({len(touching)} ticket vắt qua lần deploy bị loại)"
        return {
            **result,
            "after": _group([]),
            "before": _group([]),
            "mixed": {"tickets": len(touching), "ids": touching},
            "complete": False,
            "regressed": None,
            "reasons": [reason],
        }
    # Đợt chạy gần nhất của cấu hình này: lùi từ ticket khớp cuối cùng tới khi gặp cấu hình khác
    # (cùng nội dung company/ có thể quay lại sau một lần revert, không gộp hai đợt).
    first = last
    while first > 0 and sha_matches(started[first - 1]["company_sha"], sha):
        first -= 1
    # Theo lúc bắt đầu, KHÔNG lọc "đã kết thúc" trước khi cắt: ticket kẹt phải giữ chỗ của nó.
    after = started[first : last + 1][:n]
    before = started[max(0, first - n) : first]
    window_start = datetime.fromisoformat((before or after)[0]["started_at"])
    # Ticket bị loại đáng nêu: chạm cấu hình đang xét, hoặc bắt đầu trong khoảng hai nhóm.
    mixed = [
        r["id"]
        for r in eligible
        if _is_mixed(r)
        and (
            datetime.fromisoformat(r["started_at"]) >= window_start
            or any(sha_matches(s, sha) for s in r["company_shas"])
        )
    ]
    a, b = _group(after), _group(before)
    reasons: list[str] = []
    missing: list[str] = []  # tiêu chí không so được vì một nhóm thiếu dữ liệu
    drop = increase = non_done = None
    if a["first_pass_rate"] is not None and b["first_pass_rate"] is not None:
        drop = round((b["first_pass_rate"] - a["first_pass_rate"]) * 100, 1)
        if drop >= first_pass_drop:
            reasons.append(f"tỷ lệ đạt vòng đầu giảm {drop} điểm (ngưỡng {first_pass_drop})")
    else:
        missing.append("tỷ lệ đạt vòng đầu (có nhóm không ticket nào done)")
    if a["avg_review_rounds"] is not None and b["avg_review_rounds"] is not None:
        increase = round(a["avg_review_rounds"] - b["avg_review_rounds"], 2)
        if increase >= review_increase:
            reasons.append(f"vòng review TB tăng {increase} (ngưỡng {review_increase})")
    else:
        missing.append("vòng review TB (có nhóm không ticket nào tới review)")
    if a["non_done_rate"] is not None and b["non_done_rate"] is not None:
        non_done = round((a["non_done_rate"] - b["non_done_rate"]) * 100, 1)
        if non_done >= non_done_increase:
            reasons.append(
                f"tỷ lệ kết thúc không-done (escalated + failed) tăng {non_done} điểm "
                f"(ngưỡng {non_done_increase})"
            )
        if a["done"] == 0 and b["done"] > 0:
            reasons.append(
                f"nhóm sau không ticket nào done ({a['finished']} ticket đã kết thúc), "
                f"nhóm trước done {b['done']}/{b['finished']}"
            )
    complete = len(after) >= n and not a["pending"]
    regressed: bool | None
    if not complete or not b["finished"]:
        regressed = None
        waiting = f", còn chờ {' '.join(a['pending'])}" if a["pending"] else ""
        reasons.append(
            f"chưa đủ mẫu: nhóm sau {a['finished']}/{n} ticket đầu đã kết thúc{waiting}; "
            f"nhóm trước {b['finished']} ticket đã kết thúc"
        )
    elif reasons:
        regressed = True
    elif missing:
        regressed = None
        reasons.append("thiếu dữ liệu để so: " + "; ".join(missing))
    else:
        regressed = False
    return {
        **result,
        "after": a,
        "before": b,
        "mixed": {"tickets": len(mixed), "ids": mixed},
        "first_pass_drop": drop,
        "review_increase": increase,
        "non_done_increase": non_done,
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
    non_done_increase: float = NON_DONE_INCREASE_POINTS,
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
            non_done_increase=non_done_increase,
            code_only=code_only,
        ),
    }
