from __future__ import annotations

import json
from pathlib import Path

import pytest

from accounting.tasks.shopify_sales.aggregator import aggregate
from accounting.tasks.shopify_sales.partner_map import GatewayResolution


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "shopify_orders_2026-04.json"


def _load_orders():
    return json.loads(FIXTURE_PATH.read_text())["orders"]


def _resolver(gateway: str) -> GatewayResolution:
    """テスト用 resolver（env 不要）。"""
    if gateway == "shopify_payments":
        return GatewayResolution(
            slug="shopify_payments",
            partner_id=900001,
            partner_name="Shopify Payments",
            canonical_gateway="shopify_payments",
        )
    if gateway.startswith("KOMOJU"):
        return GatewayResolution(
            slug="komoju",
            partner_id=102026938,
            partner_name="株式会社デジカ（KOMOJU）",
            canonical_gateway="KOMOJU",
        )
    raise ValueError(f"unexpected gateway in test: {gateway}")


def test_aggregate_2026_04_fixture_matches_spec():
    """仕様書 §8 の期待値: 47件 / ¥141,949、SP 46件 / KOMOJU 1件。"""
    orders = _load_orders()
    summary = aggregate(
        year=2026,
        month=4,
        orders=orders,
        komoju_fee_rate=0.036,
        resolver=_resolver,
    )

    assert summary.order_count == 47
    assert summary.excluded_count == 3
    assert summary.total_gross == 141949
    assert summary.total_fee == 4833 + 151
    assert summary.total_net == summary.total_gross - summary.total_fee

    by_id = summary.by_partner

    sp = by_id[900001]
    assert sp.partner_name == "Shopify Payments"
    assert sp.order_count == 46
    assert sp.gross == 137764
    assert sp.fee == 4833  # API 値合算
    assert sp.net == 137764 - 4833  # = 132931

    komoju = by_id[102026938]
    assert komoju.order_count == 1
    assert komoju.gross == 4185
    # 4185 * 0.036 = 150.66 → 151（四捨五入）
    assert komoju.fee == 151
    assert komoju.net == 4034


def test_aggregate_empty_month():
    summary = aggregate(
        year=2026, month=5, orders=[], komoju_fee_rate=0.036, resolver=_resolver
    )
    assert summary.order_count == 0
    assert summary.excluded_count == 0
    assert summary.total_gross == 0
    assert summary.by_partner == {}


def test_aggregate_period_dates():
    summary = aggregate(
        year=2026, month=2, orders=[], komoju_fee_rate=0.036, resolver=_resolver
    )
    assert str(summary.period_start_jst) == "2026-02-01"
    # うるう年ではない 2026 年 → 2月末は 28
    assert str(summary.period_end_jst) == "2026-02-28"


def test_aggregate_komoju_fee_rate_override():
    """KOMOJU 手数料率を override したら追従する。"""
    order = {
        "name": "#9001",
        "tags": [],
        "cancelledAt": None,
        "displayFinancialStatus": "PAID",
        "paymentGatewayNames": ["KOMOJU - スマホ決済"],
        "totalPriceSet": {"shopMoney": {"amount": "10000.00"}},
        "totalRefundedSet": {"shopMoney": {"amount": "0.00"}},
        "transactions": [],
    }
    summary = aggregate(
        year=2026, month=4, orders=[order], komoju_fee_rate=0.05, resolver=_resolver
    )
    ps = summary.by_partner[102026938]
    assert ps.gross == 10000
    assert ps.fee == 500


def test_aggregate_refund_reduces_gross():
    """totalRefunded を引いた純額で計上される。"""
    order = {
        "name": "#9002",
        "tags": [],
        "cancelledAt": None,
        "displayFinancialStatus": "PARTIALLY_REFUNDED",
        "paymentGatewayNames": ["shopify_payments"],
        "totalPriceSet": {"shopMoney": {"amount": "5000.00"}},
        "totalRefundedSet": {"shopMoney": {"amount": "2000.00"}},
        "transactions": [
            {
                "gateway": "shopify_payments",
                "kind": "SALE",
                "status": "SUCCESS",
                "amountSet": {"shopMoney": {"amount": "5000.00"}},
                "fees": [
                    {"amount": {"amount": "175.00"}, "rate": "0.035", "type": "PAYMENT_FEE"}
                ],
            }
        ],
    }
    summary = aggregate(
        year=2026, month=4, orders=[order], komoju_fee_rate=0.036, resolver=_resolver
    )
    ps = summary.by_partner[900001]
    assert ps.gross == 3000  # 5000 - 2000
    assert ps.fee == 175


def test_aggregate_zero_or_negative_gross_excluded():
    """全額返金された Order は除外件数にカウントされる。"""
    order = {
        "name": "#9003",
        "tags": [],
        "cancelledAt": None,
        "displayFinancialStatus": "PARTIALLY_REFUNDED",
        "paymentGatewayNames": ["shopify_payments"],
        "totalPriceSet": {"shopMoney": {"amount": "5000.00"}},
        "totalRefundedSet": {"shopMoney": {"amount": "5000.00"}},
        "transactions": [],
    }
    summary = aggregate(
        year=2026, month=4, orders=[order], komoju_fee_rate=0.036, resolver=_resolver
    )
    assert summary.order_count == 0
    assert summary.excluded_count == 1


def test_aggregate_multiple_gateways_per_partner():
    """KOMOJU の複数バリエーションは同一 partner に集約され gateways に両方記録される。"""
    orders = [
        {
            "name": "#a",
            "tags": [],
            "cancelledAt": None,
            "displayFinancialStatus": "PAID",
            "paymentGatewayNames": ["KOMOJU - スマホ決済"],
            "totalPriceSet": {"shopMoney": {"amount": "1000.00"}},
            "totalRefundedSet": {"shopMoney": {"amount": "0.00"}},
            "transactions": [],
        },
        {
            "name": "#b",
            "tags": [],
            "cancelledAt": None,
            "displayFinancialStatus": "PAID",
            "paymentGatewayNames": ["KOMOJU - コンビニ決済"],
            "totalPriceSet": {"shopMoney": {"amount": "2000.00"}},
            "totalRefundedSet": {"shopMoney": {"amount": "0.00"}},
            "transactions": [],
        },
    ]
    summary = aggregate(
        year=2026, month=4, orders=orders, komoju_fee_rate=0.036, resolver=_resolver
    )
    ps = summary.by_partner[102026938]
    assert ps.order_count == 2
    assert ps.gross == 3000
    assert "KOMOJU - スマホ決済" in ps.gateways
    assert "KOMOJU - コンビニ決済" in ps.gateways
