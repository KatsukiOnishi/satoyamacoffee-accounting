"""Order list → MonthlySummary 集計ロジック。

Shopify Payments の手数料は API の transactions[].fees[].amount を合算。
KOMOJU は固定率（SHOPIFY_KOMOJU_FEE_RATE）で算出。
"""
from __future__ import annotations

import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable

from accounting.core.logger import get_logger
from accounting.tasks.shopify_sales.filters import should_exclude
from accounting.tasks.shopify_sales.models import MonthlySummary, PartnerSummary
from accounting.tasks.shopify_sales.partner_map import (
    GatewayResolution,
    PartnerNotConfiguredError,
    UnknownGatewayError,
    resolve,
)

log = get_logger("shopify-sales")


def _yen(value: str | int | float | Decimal | None) -> int:
    """Shopify の金額文字列（"4185.00" 等）を円整数（四捨五入）にする。"""
    if value is None or value == "":
        return 0
    if isinstance(value, int):
        return value
    d = Decimal(str(value))
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _money(money_set: dict[str, Any] | None, key: str = "shopMoney") -> int:
    if not money_set:
        return 0
    shop_money = money_set.get(key) or {}
    return _yen(shop_money.get("amount"))


def _order_gross(order: dict[str, Any]) -> int:
    """売上総額（税込、返金控除後）。totalPrice - totalRefunded。"""
    total = _money(order.get("totalPriceSet"))
    refunded = _money(order.get("totalRefundedSet"))
    return total - refunded


def _shopify_payments_fee(order: dict[str, Any]) -> int:
    """transactions[].fees[].amount を合算（円整数）。

    全 transactions（sale / authorization / capture）の fees を合算する。
    refund / void は fees が無いか負値で返るので、そのまま合算しても整合する。
    """
    fee_total = Decimal("0")
    for txn in order.get("transactions") or []:
        if (txn.get("status") or "").upper() not in {"SUCCESS", ""}:
            continue
        for f in txn.get("fees") or []:
            amt = (f.get("amount") or {}).get("amount")
            if amt is None:
                continue
            fee_total += Decimal(str(amt))
    return int(fee_total.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _flat_fee(gross: int, rate: float) -> int:
    """固定率手数料（円整数、四捨五入）。"""
    return int(
        (Decimal(gross) * Decimal(str(rate))).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def _last_day(year: int, month: int) -> date:
    _, last = calendar.monthrange(year, month)
    return date(year, month, last)


def aggregate(
    *,
    year: int,
    month: int,
    orders: list[dict[str, Any]],
    komoju_fee_rate: float,
    resolver: Callable[[str], GatewayResolution] = resolve,
) -> MonthlySummary:
    """orders を集計して MonthlySummary を返す。

    Args:
        year/month: JST 月
        orders: Shopify GraphQL Order ノードのリスト
        komoju_fee_rate: KOMOJU 手数料率（0.036 等）
        resolver: gateway → GatewayResolution の解決関数（テストで差し替え可）
    """
    summary = MonthlySummary(
        year=year,
        month=month,
        period_start_jst=date(year, month, 1),
        period_end_jst=_last_day(year, month),
    )

    for order in orders:
        skip, reason = should_exclude(order)
        if skip:
            summary.excluded_count += 1
            log.info(
                "shopify_sales.exclude",
                order=order.get("name"),
                reason=reason,
            )
            continue

        gateways = order.get("paymentGatewayNames") or []
        if not gateways:
            raise ValueError(
                f"Order {order.get('name')} に paymentGatewayNames が無い。"
                "Shopify 設定の確認が必要"
            )
        primary_gateway = gateways[0]
        try:
            res = resolver(primary_gateway)
        except (UnknownGatewayError, PartnerNotConfiguredError):
            raise

        gross = _order_gross(order)
        if gross <= 0:
            # 全額返金された Order は売上計上しない（純額0以下）
            summary.excluded_count += 1
            log.info(
                "shopify_sales.exclude",
                order=order.get("name"),
                reason="zero_or_negative_gross",
            )
            continue

        if res.canonical_gateway == "shopify_payments":
            fee = _shopify_payments_fee(order)
        elif res.canonical_gateway == "KOMOJU":
            fee = _flat_fee(gross, komoju_fee_rate)
        else:
            # Amazon Pay / PayPay は仕様書では4月実績ゼロ。
            # 出現したら警告を残しつつ手数料0で計上（後日 cohort で精算）。
            fee = _shopify_payments_fee(order)
            summary.warnings.append(
                f"{res.partner_name} で出現: order={order.get('name')} "
                f"gross={gross} fee={fee}（手数料は API 値のみ、要確認）"
            )

        ps = summary.by_partner.get(res.partner_id)
        if ps is None:
            ps = PartnerSummary(
                partner_id=res.partner_id, partner_name=res.partner_name
            )
            summary.by_partner[res.partner_id] = ps
        ps.order_count += 1
        ps.gross += gross
        ps.fee += fee
        ps.gateways.add(primary_gateway)
        summary.order_count += 1

    log.info(
        "shopify_sales.aggregate.complete",
        year=year,
        month=month,
        order_count=summary.order_count,
        excluded_count=summary.excluded_count,
        total_gross=summary.total_gross,
        total_fee=summary.total_fee,
        partners={
            ps.partner_name: {
                "count": ps.order_count,
                "gross": ps.gross,
                "fee": ps.fee,
            }
            for ps in summary.by_partner.values()
        },
    )
    return summary
