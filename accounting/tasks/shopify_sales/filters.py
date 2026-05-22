"""Order の除外フィルタ。

仕様書 §5-1:
1. tags に `exclude-from-accounting` を含む
2. cancelledAt が null でない
3. displayFinancialStatus が PAID でも PARTIALLY_REFUNDED でもない
"""
from __future__ import annotations

from typing import Any

EXCLUDE_TAG = "exclude-from-accounting"
COUNT_AS_SALES_STATUSES = {"PAID", "PARTIALLY_REFUNDED"}


def _normalize_tags(tags: Any) -> list[str]:
    """Shopify GraphQL の tags は list[str]、REST 由来だと CSV 文字列の場合あり。"""
    if tags is None:
        return []
    if isinstance(tags, str):
        return [t.strip() for t in tags.split(",") if t.strip()]
    return [str(t).strip() for t in tags if str(t).strip()]


def should_exclude(order: dict[str, Any]) -> tuple[bool, str | None]:
    """除外すべきなら (True, reason) を返す。除外しないなら (False, None)。"""
    tags = _normalize_tags(order.get("tags"))
    if EXCLUDE_TAG in tags:
        return True, f"tag:{EXCLUDE_TAG}"

    if order.get("cancelledAt"):
        return True, "cancelled"

    status = (order.get("displayFinancialStatus") or "").upper()
    if status not in COUNT_AS_SALES_STATUSES:
        return True, f"financial_status:{status or 'UNKNOWN'}"

    return False, None
