"""auto-classify の freee Deal 登録レイヤ（production モード時のみ呼ばれる）。

仕様書 §12-2: registrar に「mode='shadow' なら絶対に freee API を叩かない」ガードを入れる。
"""
from __future__ import annotations

from datetime import date

from accounting.connectors.freee import FreeeClient
from accounting.core.idempotency import is_executed, mark_executed
from accounting.core.logger import get_logger
from accounting.tasks.auto_classify.models import ClassificationResult

log = get_logger("auto_classify.registrar")

TASK_NAME = "auto_classify"


class ShadowModeViolation(RuntimeError):
    """shadow モードで freee 登録を試みた時の保険例外。"""


def build_external_id(wallet_txn_id: int) -> str:
    return f"auto-classify:wallet_txn:{int(wallet_txn_id)}"


def build_deal_payload(
    *,
    company_id: int,
    issue_date: date,
    is_income: bool,
    partner_id: int | None,
    account_item_id: int,
    tax_code: int,
    amount: int,
    description: str,
    from_walletable_type: str | None,
    from_walletable_id: int | None,
) -> dict:
    """type=income/expense の確定済み Deal payload を構築する。

    wallet_txn の口座情報を payments[] に直接埋めて1ショット登録する。
    """
    payload: dict = {
        "issue_date": issue_date.isoformat(),
        "type": "income" if is_income else "expense",
        "company_id": int(company_id),
        "details": [
            {
                "account_item_id": int(account_item_id),
                "tax_code": int(tax_code),
                "amount": abs(int(amount)),
                "description": description[:200],
            }
        ],
    }
    if partner_id is not None:
        payload["partner_id"] = int(partner_id)
    if from_walletable_type and from_walletable_id is not None:
        payload["payments"] = [
            {
                "date": issue_date.isoformat(),
                "from_walletable_type": from_walletable_type,
                "from_walletable_id": int(from_walletable_id),
                "amount": abs(int(amount)),
            }
        ]
    return payload


def register_deal_for_classification(
    freee: FreeeClient,
    result: ClassificationResult,
    *,
    company_id: int,
    mode: str,
    run_id: str,
) -> ClassificationResult:
    """ClassificationResult を freee に登録する。

    Args:
        mode: 'production' でのみ実 API を叩く。'shadow' なら ShadowModeViolation を上げる。
    """
    if mode == "shadow":
        raise ShadowModeViolation(
            "shadow mode must not call freee — registrar はモード分岐を呼び出し側で行うこと"
        )

    if result.output is None:
        result.action_taken = "failed"
        result.error_message = "no classification output"
        return result
    if result.resolved_account_item_id is None:
        result.action_taken = "review_required"
        result.error_message = (
            f"account_item_name '{result.output.account_item_name}' "
            "not found in freee master"
        )
        return result
    if result.resolved_tax_code_id is None:
        result.action_taken = "review_required"
        result.error_message = (
            f"tax_code_name '{result.output.tax_code_name}' "
            "not found in freee tax master"
        )
        return result

    txn = result.wallet_txn
    is_income = (txn.entry_side == "income") or txn.amount > 0

    external_id = build_external_id(txn.id)
    if is_executed(TASK_NAME, external_id):
        result.action_taken = "registered"
        result.error_message = "already executed (idempotency)"
        return result

    payload = build_deal_payload(
        company_id=company_id,
        issue_date=txn.date,
        is_income=is_income,
        partner_id=result.resolved_partner_id,
        account_item_id=result.resolved_account_item_id,
        tax_code=result.resolved_tax_code_id,
        amount=txn.amount,
        description=txn.description[:200],
        from_walletable_type=txn.walletable_type,
        from_walletable_id=txn.walletable_id,
    )

    try:
        res = freee.create_deal(
            payload, external_id=external_id, task=TASK_NAME
        )
        if res.get("dry_run"):
            result.action_taken = "registered"  # dry-run でも「登録予定」扱い
            return result
        deal_id = res.get("deal_id")
        result.freee_deal_id = int(deal_id) if deal_id else None
        result.action_taken = "registered"
        mark_executed(
            TASK_NAME, external_id, run_id, str(deal_id or ""), "success"
        )
        return result
    except Exception as e:
        log.exception(
            "auto_classify.register_failed",
            wallet_txn_id=txn.id,
            payload=payload,
        )
        mark_executed(TASK_NAME, external_id, run_id, None, "failed")
        result.action_taken = "failed"
        result.error_message = f"{type(e).__name__}: {e}"
        return result
