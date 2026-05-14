"""Resend で実行結果を要約してメール送信する。

`accounting.core.notifier._send` を直接借用すると secret 経路が共通化できるので
そちらに合わせて構築する。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Iterable

from accounting.core.logger import get_logger
from accounting.core.notifier import _send as _resend_send
from accounting.core.vendor_invoice_candidates import VendorInvoiceCandidate

logger = get_logger("vendor_invoice.notifier")


def _fmt_yen(n: int | None) -> str:
    if n is None:
        return "(金額不明)"
    return f"¥{n:,}"


def _fmt_due(d: date | None) -> str:
    return d.isoformat() if d else "(期日不明)"


def _line_for(c: VendorInvoiceCandidate) -> str:
    name = c.extracted_partner_name or c.sender or "(不明)"
    amount = _fmt_yen(c.extracted_amount)
    due = _fmt_due(c.extracted_due_date)
    suffix_parts: list[str] = [f"sender={c.sender}"]
    if c.extracted_issue_date:
        suffix_parts.append(f"発行={c.extracted_issue_date}")
    if c.freee_deal_id:
        suffix_parts.append(f"deal={c.freee_deal_id}")
    if c.reconciled_with_deal_id:
        suffix_parts.append(f"matched_deal={c.reconciled_with_deal_id}")
    if c.exclusion_reason:
        suffix_parts.append(f"reason={c.exclusion_reason}")
    return (
        f"  - {name} {amount} (期日 {due}) [id={c.id}] "
        f"{'/'.join(suffix_parts)}"
    )


def build_summary(
    *,
    run_id: str,
    scan_days: int,
    candidates: Iterable[VendorInvoiceCandidate],
    dry_run: bool,
    executed_at: datetime | None = None,
) -> tuple[str, str]:
    """件名と本文を返す。"""
    executed_at = executed_at or datetime.now()
    buckets: dict[str, list[VendorInvoiceCandidate]] = defaultdict(list)
    for c in candidates:
        buckets[c.status].append(c)

    reconciled = buckets.get("reconciled", [])
    unpaid = buckets.get("unpaid", []) + buckets.get("registered", [])
    review = buckets.get("manual_review", []) + buckets.get("failed", [])
    encrypted = [c for c in review if c.classification == "encrypted_zip"]
    other_review = [c for c in review if c.classification != "encrypted_zip"]
    excluded = buckets.get("excluded", [])
    pending = buckets.get("pending", [])

    dry_run_tag = "[DRY-RUN] " if dry_run else ""
    subject = (
        f"[vendor-invoice] {dry_run_tag}{executed_at:%Y-%m-%d} "
        f"消し込み{len(reconciled)} / 未払{len(unpaid)} / 要確認{len(other_review) + len(encrypted)}"
    )

    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("vendor-invoice タスク実行レポート")
    lines.append(f"実行日時: {executed_at:%Y-%m-%d %H:%M:%S}")
    lines.append(f"run_id  : {run_id}")
    lines.append(f"スキャン期間: 過去 {scan_days} 日")
    if dry_run:
        lines.append("実行モード: DRY-RUN（freee には何も書き込んでいません）")
    lines.append("=" * 60)

    lines.append("")
    lines.append(f"【消し込み完了】{len(reconciled)}件")
    for c in reconciled:
        lines.append(_line_for(c))

    lines.append("")
    lines.append(f"【未払（要振込）】{len(unpaid)}件")
    for c in unpaid:
        lines.append(_line_for(c))
    if unpaid:
        lines.append(
            "  👉 振込実行後、`accounting vendor-invoice reconcile` で消し込み再試行"
        )

    if pending:
        lines.append("")
        lines.append(f"【dry-run で処理予定】{len(pending)}件")
        for c in pending:
            lines.append(_line_for(c))

    if encrypted:
        lines.append("")
        lines.append(f"【暗号化ZIP（手動確認）】{len(encrypted)}件")
        for c in encrypted:
            lines.append(_line_for(c))

    if other_review:
        lines.append("")
        lines.append(f"【要手動確認】{len(other_review)}件")
        for c in other_review:
            lines.append(_line_for(c))
            if c.error_message:
                lines.append(f"    error: {c.error_message[:200]}")
        lines.append(
            "  👉 `accounting vendor-invoice list` で詳細 → "
            "`accounting vendor-invoice apply <id>` で処理"
        )

    if excluded:
        lines.append("")
        lines.append(f"【除外】{len(excluded)}件（参考、詳細省略）")

    lines.append("")
    lines.append("=" * 60)
    return subject, "\n".join(lines)


def notify_run_summary(
    *,
    run_id: str,
    scan_days: int,
    candidates: Iterable[VendorInvoiceCandidate],
    dry_run: bool,
) -> None:
    subject, body = build_summary(
        run_id=run_id, scan_days=scan_days, candidates=candidates, dry_run=dry_run
    )
    _resend_send(subject, body)
    logger.info("vendor_invoice.notify.sent", subject=subject)
