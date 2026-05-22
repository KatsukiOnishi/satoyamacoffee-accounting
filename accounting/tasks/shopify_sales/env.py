"""shopify-sales 専用 env リーダ。

config.py を肥大化させないため、本タスク固有の env はここで os.environ から
直接読む（既存 dept_store_invoice の vendor_partner_id と同じ思想）。

| env 名 | 用途 | デフォルト |
|---|---|---|
| `SHOPIFY_KOMOJU_FEE_RATE` | KOMOJU 手数料率 | `0.036` |
| `SHOPIFY_SALES_TAX_CODE_SALES_REDUCED_8` | 軽減税率8%売上 | `156` |
| `SHOPIFY_SALES_TAX_CODE_OUT_OF_SCOPE` | 対象外 | `2` |
| `SHOPIFY_SALES_TAX_CODE_NONE` | 税区分なし（売掛金等の決済勘定） | `0` |
| `SHOPIFY_SALES_ACCOUNT_RECEIVABLE_ID` | 売掛金 ID | 未設定なら freee API から name で引く |
| `SHOPIFY_SALES_ACCOUNT_SALES_ID` | 売上高 ID | 同上 |
| `SHOPIFY_SALES_ACCOUNT_COMMISSION_ID` | 支払手数料 ID | 同上 |
| `SHOPIFY_SALES_PARTNER_<SLUG>` | `<partner_id>,<display_name>` | 必須 |
"""
from __future__ import annotations

import os

from accounting.config import settings


def komoju_fee_rate() -> float:
    raw = os.environ.get("SHOPIFY_KOMOJU_FEE_RATE", "").strip()
    if not raw:
        return 0.036
    return float(raw)


def tax_code_sales_reduced_8() -> int:
    return int(os.environ.get("SHOPIFY_SALES_TAX_CODE_SALES_REDUCED_8", "156"))


def tax_code_out_of_scope() -> int:
    return int(os.environ.get("SHOPIFY_SALES_TAX_CODE_OUT_OF_SCOPE", "2"))


def tax_code_none() -> int:
    return int(os.environ.get("SHOPIFY_SALES_TAX_CODE_NONE", "0"))


def _account_id(name: str) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else 0


def account_receivable_id() -> int:
    return _account_id("SHOPIFY_SALES_ACCOUNT_RECEIVABLE_ID")


def account_sales_id() -> int:
    return _account_id("SHOPIFY_SALES_ACCOUNT_SALES_ID")


def account_commission_id() -> int:
    return _account_id("SHOPIFY_SALES_ACCOUNT_COMMISSION_ID")


def shopify_partner(slug: str) -> tuple[int, str] | None:
    """`SHOPIFY_SALES_PARTNER_<SLUG>=<id>,<name>` から取り出す。

    未設定 or プレースホルダ (`__...__`) なら None。
    """
    key = f"SHOPIFY_SALES_PARTNER_{slug.upper().replace('-', '_').replace(' ', '_')}"
    raw = os.environ.get(key, "").strip()
    if not raw or raw.startswith("__") or raw.endswith("__"):
        return None
    if "," not in raw:
        raise ValueError(
            f"{key} は '<partner_id>,<display_name>' 形式で指定してください: {raw!r}"
        )
    id_str, name = raw.split(",", 1)
    try:
        pid = int(id_str.strip())
    except ValueError as e:
        raise ValueError(f"{key} の partner_id が整数ではありません: {id_str!r}") from e
    return pid, name.strip()


def company_id() -> int:
    if not settings.freee_company_id:
        raise ValueError("FREEE_COMPANY_ID が未設定です")
    return int(settings.freee_company_id)
