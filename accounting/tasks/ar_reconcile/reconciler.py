"""ar-reconcile の freee 消込実行レイヤ。

freee API: POST /api/1/deals/{id}/payments で未決済 deal に支払いを追加することで
消込状態（settled）にする。dry-run は freee コネクタ側で透過処理される。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from accounting.connectors.freee import FreeeClient
from accounting.core.idempotency import is_executed, mark_executed
from accounting.core.logger import get_logger
from accounting.tasks.ar_reconcile.models import ARMatchCandidate

log = get_logger("ar_reconcile.reconciler")

TASK_NAME = "ar_reconcile"


def build_external_id(wallet_txn_id: int) -> str:
    return f"ar-reconcile:wallet_txn:{int(wallet_txn_id)}"


def reconcile_match(
    freee: FreeeClient,
    candidate: ARMatchCandidate,
    *,
    run_id: str,
) -> ARMatchCandidate:
    """status='matched' な候補を freee 上で消込実行。冪等性ガード付き。

    成功 → status='reconciled'、失敗 → status='failed'。
    候補が0 or 2件以上のものは呼び出し側でそのまま記録すること（本関数では触らない）。
    """
    if candidate.status != "matched":
        return candidate
    if not candidate.candidates:
        candidate.status = "unmatched"
        return candidate
    txn = candidate.wallet_txn
    invoice = candidate.candidates[0]

    external_id = build_external_id(txn.id)
    if is_executed(TASK_NAME, external_id):
        log.info(
            "ar_reconcile.idempotency_skip",
            external_id=external_id,
            wallet_txn_id=txn.id,
        )
        candidate.status = "reconciled"
        return candidate

    if not txn.walletable_type or txn.walletable_id is None:
        candidate.status = "failed"
        candidate.error_message = "wallet_txn lacks walletable_type / walletable_id"
        return candidate

    try:
        result = freee.create_payment_for_deal(
            deal_id=invoice.deal_id,
            payment_date=txn.date.isoformat(),
            from_walletable_type=str(txn.walletable_type),
            from_walletable_id=int(txn.walletable_id),
            amount=int(txn.amount),
            external_id=external_id,
            task=TASK_NAME,
        )
        candidate.freee_response = _coerce_response(result)
        if result.get("dry_run"):
            # dry-run は executed_operations に書かない
            candidate.status = "reconciled"
            return candidate
        mark_executed(
            TASK_NAME,
            external_id,
            run_id,
            str(result.get("payment_id") or ""),
            "success",
        )
        candidate.status = "reconciled"
        return candidate
    except Exception as e:
        log.exception(
            "ar_reconcile.payment_failed",
            wallet_txn_id=txn.id,
            deal_id=invoice.deal_id,
        )
        mark_executed(TASK_NAME, external_id, run_id, None, "failed")
        candidate.status = "failed"
        candidate.error_message = f"{type(e).__name__}: {e}"
        return candidate


def _coerce_response(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        # JSON シリアライズ可能な範囲だけ残す
        try:
            json.dumps(value, default=str, ensure_ascii=False)
            return value
        except Exception:
            return {"_repr": str(value)[:1000]}
    return {"_repr": str(value)[:1000]}


def serialize_for_db(candidate: ARMatchCandidate, *, run_id: str) -> dict[str, Any]:
    """ARMatchCandidate を ar_reconcile_candidates テーブル INSERT 用の dict に変換。"""
    txn = candidate.wallet_txn
    inv = candidate.candidates[0] if candidate.candidates else None
    return dict(
        run_id=run_id,
        run_started_at=datetime.utcnow(),
        wallet_txn_id=int(txn.id),
        wallet_txn_date=txn.date,
        wallet_txn_description=txn.description,
        wallet_txn_amount=int(txn.amount),
        matched_invoice_id=inv.deal_id if inv else None,
        matched_partner_id=inv.partner_id if inv else None,
        matched_partner_name=inv.partner_name if inv else None,
        matched_invoice_amount=inv.total_amount if inv else None,
        matched_invoice_issue_date=inv.issue_date if inv else None,
        matched_invoice_due_date=inv.due_date if inv else None,
        status=candidate.status,
        freee_reconcile_response=(
            json.dumps(candidate.freee_response, default=str, ensure_ascii=False)
            if candidate.freee_response
            else None
        ),
        error_message=candidate.error_message,
    )
