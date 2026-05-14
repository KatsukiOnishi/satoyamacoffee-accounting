"""未払金消し込みロジック。

freee には「Deal A（未払計上）と Deal B（銀行振込）を未払金で reconcile する」
専用 API は提供されていない（取引登録時の payments 配列で同時計上するか、
事後に取引画面で手動消し込みするのが本道）。

そこで本ハブの運用としては:

1. Deal A 登録直後 / または reconcile コマンド実行時に、
   `wallet_txns` から (同 partner_id, 同 amount, due_date±15日) で振込トランザクションを探す
2. 対応する Deal B が既に freee 側に存在し、かつ Cr.普通預金 / Dr.未払金 で計上されていれば、
   候補テーブルの reconciled_with_deal_id にその id を記録して status='reconciled'
3. それでも振込が見つからなければ status='unpaid' のまま

これは「freee 上のデータ整合性を完全に保つ」ことではなく
「ユーザーが月次レポートで未払 vs 振込済 を判別できるようにする」ためのトラッキング。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from accounting.connectors.freee import FreeeClient
from accounting.core.logger import get_logger
from accounting.core.vendor_invoice_candidates import (
    VendorInvoiceCandidate,
    list_by_status,
    update_status,
)

logger = get_logger("vendor_invoice.reconciler")

DEFAULT_WINDOW_DAYS = 15


@dataclass
class ReconcileResult:
    candidate_id: int
    matched_deal_id: int | None
    matched: bool
    notes: str = ""


def _find_matching_payment_deal(
    freee: FreeeClient,
    *,
    partner_id: int,
    amount: int,
    anchor_date: date,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict[str, Any] | None:
    """anchor_date±window_days で expense 種別 deals を引き、同 partner / 同金額を探す。

    freee の wallet_txns でも判定できるが、deals 経由のほうが
    partner_id・amount が確実に取れるのでこちらを優先する。
    """
    start = (anchor_date - timedelta(days=window_days)).isoformat()
    end = (anchor_date + timedelta(days=window_days)).isoformat()
    deals = freee.list_deals(start_issue_date=start, end_issue_date=end)
    for d in deals:
        if int(d.get("partner_id") or 0) != partner_id:
            continue
        if int(d.get("amount") or 0) != amount:
            continue
        # 普通預金支払 = walletable_type=bank_account & status=settled
        if d.get("type") != "expense":
            continue
        if not (d.get("from_walletable_id") or d.get("from_walletable_type")):
            # 銀行口座支払いとして登録されてるはず（freee自動同期）
            continue
        return d
    return None


def reconcile_candidate(
    freee: FreeeClient, candidate: VendorInvoiceCandidate
) -> ReconcileResult:
    """1件の未払候補について消し込み試行。"""
    if candidate.status not in ("registered", "unpaid"):
        return ReconcileResult(
            candidate.id, None, False, f"skip_status={candidate.status}"
        )
    if candidate.freee_partner_id is None or candidate.extracted_amount is None:
        return ReconcileResult(
            candidate.id, None, False, "missing_partner_or_amount"
        )

    anchor = candidate.extracted_due_date or candidate.extracted_issue_date or date.today()
    found = _find_matching_payment_deal(
        freee,
        partner_id=candidate.freee_partner_id,
        amount=candidate.extracted_amount,
        anchor_date=anchor,
    )
    if found is None:
        update_status(candidate.id, "unpaid")
        return ReconcileResult(candidate.id, None, False, "no_matching_payment")

    deal_id = int(found["id"])
    if deal_id == (candidate.freee_deal_id or 0):
        # 自分自身にマッチしたケース（API の探索条件が緩い時の保険）
        update_status(candidate.id, "unpaid")
        return ReconcileResult(candidate.id, None, False, "self_match_skip")

    update_status(
        candidate.id,
        "reconciled",
        reconciled_with_deal_id=deal_id,
    )
    logger.info(
        "vendor_invoice.reconciled",
        candidate_id=candidate.id,
        deal_a=candidate.freee_deal_id,
        deal_b=deal_id,
    )
    return ReconcileResult(candidate.id, deal_id, True, f"matched_at={found.get('issue_date')}")


def reconcile_pending() -> list[ReconcileResult]:
    """status='registered' / 'unpaid' を全部走査して消し込み試行する。"""
    pending = list_by_status(["registered", "unpaid"])
    if not pending:
        return []
    results: list[ReconcileResult] = []
    with FreeeClient() as freee:
        for c in pending:
            try:
                results.append(reconcile_candidate(freee, c))
            except Exception as e:
                logger.error(
                    "vendor_invoice.reconcile_failed",
                    candidate_id=c.id,
                    error=str(e),
                )
                results.append(
                    ReconcileResult(c.id, None, False, f"error={e}")
                )
    return results
