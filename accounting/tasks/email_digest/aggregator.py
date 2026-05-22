"""auto-keiri の週次ダイジェスト集計。

直近7日（または指定 ISO 週）の ar_reconcile_candidates / auto_classify_candidates
を読み出し、WeeklyDigest にまとめる。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from accounting.core import auto_keiri
from accounting.core.logger import get_logger
from accounting.tasks.email_digest.models import (
    ARLine,
    ClassifyLine,
    WeeklyDigest,
)

log = get_logger("email_digest.aggregator")


def iso_week_label(d: date) -> str:
    """'2026-W21' を返す。"""
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_range_for(today: date | None = None) -> tuple[date, date]:
    """月曜起点〜日曜終わりの ISO 週レンジを返す（仕様書 §5-5 月曜〜日曜 JST）。"""
    today = today or date.today()
    # isocalendar の weekday: 1=Mon, 7=Sun
    monday = today - timedelta(days=today.isocalendar()[2] - 1)
    sunday = monday + timedelta(days=6)
    return monday, sunday


def week_range_from_iso(label: str) -> tuple[date, date]:
    """'2026-W21' から月曜〜日曜を返す。"""
    if "W" not in label:
        raise ValueError("形式は 'YYYY-Www'（例: 2026-W21）")
    y, w = label.split("-W", 1)
    monday = date.fromisocalendar(int(y), int(w), 1)
    sunday = monday + timedelta(days=6)
    return monday, sunday


def aggregate(
    week_start: date,
    week_end: date,
    *,
    mode: str | None = None,
) -> WeeklyDigest:
    """期間内のレコードを集計して WeeklyDigest を返す。"""
    mode = mode or auto_keiri.get_auto_classify_mode()
    digest = WeeklyDigest(
        week_start=week_start,
        week_end=week_end,
        iso_week=iso_week_label(week_start),
        mode=mode,
    )

    ar_rows = auto_keiri.list_ar_candidates_in_range(week_start, week_end)
    for r in ar_rows:
        line = ARLine(
            partner_name=r.matched_partner_name,
            amount=int(r.wallet_txn_amount),
            issue_date=r.matched_invoice_issue_date,
            txn_date=r.wallet_txn_date,
            status=r.status,
            description=r.wallet_txn_description,
            deal_id=r.matched_invoice_id,
        )
        if r.status == "reconciled":
            digest.ar_reconciled.append(line)
        elif r.status == "unmatched":
            digest.ar_unmatched.append(line)
        elif r.status == "multiple_matches":
            digest.ar_multiple_matches.append(line)
        elif r.status == "failed":
            digest.ar_failed.append(line)

    classify_rows = auto_keiri.list_classify_candidates_in_range(week_start, week_end)
    for r in classify_rows:
        line = ClassifyLine(
            date=r.wallet_txn_date,
            description=r.wallet_txn_description or "",
            amount=int(r.wallet_txn_amount),
            account_item=r.classified_account_item_name,
            tax_code=r.classified_tax_code_name,
            confidence=float(r.classification_confidence or 0.0),
            action_taken=r.action_taken,
            reason=r.classification_reason,
        )
        if r.action_taken == "registered":
            digest.classify_registered.append(line)
        elif r.action_taken == "review_required":
            digest.classify_review_required.append(line)
        elif r.action_taken == "skipped":
            digest.classify_skipped.append(line)
        elif r.action_taken == "shadow_logged":
            digest.classify_shadow_logged.append(line)
        elif r.action_taken == "failed":
            digest.classify_failed.append(line)

    log.info(
        "email_digest.aggregated",
        week=digest.iso_week,
        mode=mode,
        ar_reconciled=len(digest.ar_reconciled),
        ar_unmatched=len(digest.ar_unmatched),
        classify_registered=len(digest.classify_registered),
        classify_shadow_logged=len(digest.classify_shadow_logged),
    )
    return digest
