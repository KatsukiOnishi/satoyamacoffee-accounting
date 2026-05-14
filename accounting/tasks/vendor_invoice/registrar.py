"""候補レコードを freee Deal（取引）として登録する。

「Dr.費用 / Cr.未払金」の取引を freee に立てる。
freee API: POST /api/1/deals
"""
from __future__ import annotations

from datetime import date
from typing import Any

from accounting.connectors.freee import FreeeClient
from accounting.core.idempotency import is_executed, mark_executed
from accounting.core.logger import get_logger

logger = get_logger("vendor_invoice.registrar")

TASK_NAME = "vendor_invoice"


def build_external_id(gmail_message_id: str, gmail_attachment_id: str | None) -> str:
    """冪等性キー。"""
    att = gmail_attachment_id or "body"
    return f"vendor-invoice:{gmail_message_id}:{att}"


def build_deal_payload(
    *,
    company_id: int,
    partner_id: int,
    issue_date: date,
    due_date: date | None,
    total_amount: int,
    expense_account_item_id: int,
    tax_code: int,
    description: str,
) -> dict[str, Any]:
    """freee Deal payload（type=expense）を組み立てる。

    freee API では partner_id を指定すれば「Cr.未払金（買掛金扱い）」が
    自動で対になるが、payment_date を未設定で渡せば「未決済」状態の取引になる。

    Reference: POST /api/1/deals  type="expense"
    """
    payload: dict[str, Any] = {
        "issue_date": issue_date.isoformat(),
        "type": "expense",
        "company_id": int(company_id),
        "partner_id": int(partner_id),
        "details": [
            {
                "account_item_id": int(expense_account_item_id),
                "tax_code": int(tax_code),
                "amount": int(total_amount),
                "description": description[:200],
            }
        ],
    }
    if due_date is not None:
        payload["due_date"] = due_date.isoformat()
    return payload


def register_deal(
    freee: FreeeClient,
    payload: dict[str, Any],
    external_id: str,
    run_id: str,
) -> dict[str, Any]:
    """freee に取引を登録する。冪等性ガードあり、dry-run 透過。"""
    if is_executed(TASK_NAME, external_id):
        return {"skipped": True, "reason": "already_executed", "external_id": external_id}

    result = freee.create_deal(payload, external_id=external_id, task=TASK_NAME)
    if result.get("dry_run"):
        # dry-run は executed_operations に書かない（rehearsal を本番idempotencyに混ぜない）
        return result

    deal_id = result.get("deal_id")
    mark_executed(TASK_NAME, external_id, run_id, str(deal_id or ""), "success")
    logger.info(
        "vendor_invoice.deal_registered",
        external_id=external_id,
        deal_id=deal_id,
    )
    return result
