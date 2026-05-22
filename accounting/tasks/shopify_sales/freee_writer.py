"""MonthlySummary → freee manual_journal（振替伝票）payload を構築する。

仕訳:
  借方  売掛金   partner=Shopify Payments  amount=net
  借方  売掛金   partner=KOMOJU            amount=net
  借方  支払手数料 partner=Shopify Payments  amount=fee  (>0 のみ)
  借方  支払手数料 partner=KOMOJU            amount=fee  (>0 のみ)
  貸方  売上高 (軽減税率8% 内税)            amount=total_gross

仕様書 §6 では `POST /api/1/deals` と記載されているが、freee deals API は
全 details が同じ entry_side である取引（income/expense）にしか対応していない。
複合仕訳（借方/貸方 双方に複数行）は `POST /api/1/manual_journals`
（振替伝票）が正解。payroll タスクと同じ実装パターンに揃える。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from accounting.connectors.freee import FreeeClient
from accounting.core.logger import get_logger
from accounting.tasks.shopify_sales import env as _env
from accounting.tasks.shopify_sales.models import MonthlySummary

log = get_logger("shopify-sales")

ACCOUNT_NAME_RECEIVABLE = "売掛金"
ACCOUNT_NAME_SALES = "売上高"
ACCOUNT_NAME_COMMISSION = "支払手数料"


@dataclass
class AccountIds:
    receivable: int
    sales: int
    commission: int


def resolve_account_ids(
    *,
    account_items: list[dict[str, Any]] | None = None,
    freee: FreeeClient | None = None,
) -> AccountIds:
    """env で上書きされていればそれを優先、なければ freee API から name で引く。"""
    rec = _env.account_receivable_id()
    sales = _env.account_sales_id()
    com = _env.account_commission_id()
    if rec and sales and com:
        return AccountIds(receivable=rec, sales=sales, commission=com)

    if account_items is None:
        if freee is None:
            raise ValueError(
                "account_items も freee も渡されていません。どちらかを指定してください"
            )
        account_items = freee.get_account_items()

    name_to_id: dict[str, int] = {}
    for it in account_items:
        n = it.get("name")
        i = it.get("id")
        if n and isinstance(i, int):
            name_to_id[n] = i

    def find(target: str) -> int:
        if target in name_to_id:
            return name_to_id[target]
        raise ValueError(
            f"freee 勘定科目 '{target}' が見つかりません。"
            "freee 画面で勘定科目を作成するか、SHOPIFY_SALES_ACCOUNT_*_ID を env で指定してください"
        )

    return AccountIds(
        receivable=rec or find(ACCOUNT_NAME_RECEIVABLE),
        sales=sales or find(ACCOUNT_NAME_SALES),
        commission=com or find(ACCOUNT_NAME_COMMISSION),
    )


def build_external_id(year: int, month: int) -> str:
    return f"shopify-sales:{year:04d}-{month:02d}"


def build_manual_journal_payload(
    *,
    summary: MonthlySummary,
    company_id: int,
    account_ids: AccountIds,
) -> dict[str, Any]:
    """月次 1 本の manual_journal（振替伝票）payload を組み立てる。

    借方=貸方の整合性を保証する。partner_id は明細行に直接載せる。
    """
    if summary.order_count == 0:
        raise ValueError(
            f"{summary.year}-{summary.month:02d} の集計対象 Order が 0 件です。Deal を作成できません"
        )

    issue_date = summary.period_end_jst.isoformat()
    details: list[dict[str, Any]] = []

    # ---- 貸方: 売上高 ----
    details.append(
        {
            "account_item_id": account_ids.sales,
            "tax_code": _env.tax_code_sales_reduced_8(),
            "amount": summary.total_gross,
            "entry_side": "credit",
            "description": (
                f"Shopify売上 {summary.year}-{summary.month:02d}"
                f" ({summary.order_count}件)"
            ),
        }
    )

    # ---- 借方: 売掛金 (partner 別) ----
    for ps in summary.by_partner.values():
        details.append(
            {
                "account_item_id": account_ids.receivable,
                "tax_code": _env.tax_code_none(),
                "partner_id": ps.partner_id,
                "amount": ps.net,
                "entry_side": "debit",
                "description": (
                    f"{ps.partner_name} {ps.order_count}件 純額"
                ),
            }
        )

    # ---- 借方: 支払手数料 (partner 別) ----
    for ps in summary.by_partner.values():
        if ps.fee <= 0:
            continue
        details.append(
            {
                "account_item_id": account_ids.commission,
                "tax_code": _env.tax_code_out_of_scope(),
                "partner_id": ps.partner_id,
                "amount": ps.fee,
                "entry_side": "debit",
                "description": f"{ps.partner_name} 決済手数料",
            }
        )

    # 借方=貸方 チェック
    debits = sum(d["amount"] for d in details if d["entry_side"] == "debit")
    credits = sum(d["amount"] for d in details if d["entry_side"] == "credit")
    if debits != credits:
        raise ValueError(
            f"借方({debits}) と 貸方({credits}) が一致しません。"
            f"summary={summary!r}"
        )

    return {
        "company_id": company_id,
        "issue_date": issue_date,
        "details": details,
    }
