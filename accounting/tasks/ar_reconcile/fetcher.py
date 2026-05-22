"""ar-reconcile 用の freee 取得層。

責務:
  - 未消込 wallet_txn（type=income）を取得
  - 未決済請求書（deal type=income, status=unsettled）を取得
  - 上記2つを wallet_txn の支払い情報（payments）と突き合わせて
    「消込候補（freee 未処理 income wallet_txn）」のみを残す
"""
from __future__ import annotations

from datetime import date
from typing import Any

from accounting.connectors.freee import FreeeClient
from accounting.core.logger import get_logger
from accounting.tasks.ar_reconcile.models import UnsettledInvoice, WalletTxnIncome

log = get_logger("ar_reconcile.fetcher")


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def fetch_unreconciled_income_wallet_txns(
    freee: FreeeClient,
    *,
    start_date: date,
    end_date: date,
) -> list[WalletTxnIncome]:
    """期間内の入金 wallet_txn のうち、まだ deal に紐付いていないものを返す。

    判定方法（自動で経理の「未処理」相当）:
      1. wallet_txn を全件取得
      2. 同期間の deals.payments[] を全件取得して
         (walletable_type, walletable_id, date, amount) でインデックス化
      3. wallet_txn 側がそのインデックスに含まれていなければ「未紐付（=未処理）」
    """
    # walletables を全件回す（freee 仕様により walletable_type+id 同時指定が必要）
    walletables = freee.list_walletables()
    txns: list[dict[str, Any]] = []
    for w in walletables:
        chunk = freee.list_wallet_txns(
            walletable_type=w.get("type"),
            walletable_id=int(w.get("id")),
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            entry_side="income",
        )
        for t in chunk:
            t["_walletable_name"] = w.get("name")
        txns.extend(chunk)

    # deals の payments を index 化（settled / unsettled の両方を見る）
    deals = freee.list_deals(
        start_issue_date=start_date.isoformat(),
        end_issue_date=end_date.isoformat(),
    )
    payment_index: set[tuple] = set()
    for d in deals:
        for p in d.get("payments") or []:
            payment_index.add(
                (
                    p.get("from_walletable_type"),
                    p.get("from_walletable_id"),
                    p.get("date"),
                    int(p.get("amount") or 0),
                )
            )

    unreconciled: list[WalletTxnIncome] = []
    for t in txns:
        key = (
            t.get("walletable_type"),
            t.get("walletable_id"),
            t.get("date"),
            int(t.get("amount") or 0),
        )
        if key in payment_index:
            continue
        d_ = _parse_date(t.get("date"))
        if d_ is None:
            continue
        unreconciled.append(
            WalletTxnIncome(
                id=int(t["id"]),
                date=d_,
                description=t.get("description") or "",
                amount=int(t.get("amount") or 0),
                walletable_type=t.get("walletable_type"),
                walletable_id=t.get("walletable_id"),
                walletable_name=t.get("_walletable_name"),
                entry_side=t.get("entry_side"),
            )
        )
    log.info(
        "ar_reconcile.fetcher.income_wallet_txns",
        total_walletables=len(walletables),
        total_txns=len(txns),
        unreconciled=len(unreconciled),
    )
    return unreconciled


def fetch_unreconciled_wallet_txns_all(
    freee: FreeeClient,
    *,
    start_date: date,
    end_date: date,
) -> list[dict[str, Any]]:
    """auto-classify 用: 入金/出金 両方の未紐付 wallet_txn を生 dict のまま返す。

    walletable_name を `_walletable_name` フィールドで付与する。
    """
    walletables = freee.list_walletables()
    txns: list[dict[str, Any]] = []
    for w in walletables:
        chunk = freee.list_wallet_txns(
            walletable_type=w.get("type"),
            walletable_id=int(w.get("id")),
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
        )
        for t in chunk:
            t["_walletable_name"] = w.get("name")
        txns.extend(chunk)

    deals = freee.list_deals(
        start_issue_date=start_date.isoformat(),
        end_issue_date=end_date.isoformat(),
    )
    payment_index: set[tuple] = set()
    for d in deals:
        for p in d.get("payments") or []:
            payment_index.add(
                (
                    p.get("from_walletable_type"),
                    p.get("from_walletable_id"),
                    p.get("date"),
                    int(p.get("amount") or 0),
                )
            )

    unreconciled: list[dict[str, Any]] = []
    for t in txns:
        key = (
            t.get("walletable_type"),
            t.get("walletable_id"),
            t.get("date"),
            int(t.get("amount") or 0),
        )
        if key in payment_index:
            continue
        unreconciled.append(t)
    log.info(
        "ar_reconcile.fetcher.all_wallet_txns",
        total_walletables=len(walletables),
        total_txns=len(txns),
        unreconciled=len(unreconciled),
    )
    return unreconciled


def fetch_unsettled_income_invoices(
    freee: FreeeClient,
    *,
    start_date: date,
    end_date: date,
    partner_map: dict[int, str] | None = None,
) -> list[UnsettledInvoice]:
    """未決済の売上 deal を返す（消込引き当て候補プール）。

    partner_map は { partner_id: partner_name } で、deals API が partner_name を
    返さない事業所での補完用。None なら deal の partner_name フィールドを直接見る。
    """
    deals = freee.list_deals(
        start_issue_date=start_date.isoformat(),
        end_issue_date=end_date.isoformat(),
        deal_type="income",
        status="unsettled",
    )
    out: list[UnsettledInvoice] = []
    for d in deals:
        partner_id = d.get("partner_id")
        name = d.get("partner_name") or (
            (partner_map or {}).get(int(partner_id)) if partner_id else None
        )
        # 仕様書 §5-1: amount は freee 「請求書」概念のところは total_amount に近い。
        # deals は内部的に details[].amount の合計 = deal.total_amount だが、
        # 入金引き当てのキーは「請求書の総額」なので deal.amount フィールドを使う。
        total = int(d.get("amount") or 0) or sum(
            int(x.get("amount") or 0) for x in (d.get("details") or [])
        )
        out.append(
            UnsettledInvoice(
                deal_id=int(d["id"]),
                partner_id=int(partner_id) if partner_id else None,
                partner_name=(name or "").strip() or None,
                total_amount=total,
                issue_date=_parse_date(d.get("issue_date")),
                due_date=_parse_date(d.get("due_date")),
            )
        )
    log.info("ar_reconcile.fetcher.unsettled_invoices", count=len(out))
    return out
