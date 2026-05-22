from __future__ import annotations

import pytest

from accounting.tasks.shopify_sales import partner_map
from accounting.tasks.shopify_sales.partner_map import (
    PartnerNotConfiguredError,
    UnknownGatewayError,
    resolve,
    resolve_partner_slug,
)


def test_resolve_slug_shopify_payments():
    slug, canonical = resolve_partner_slug("shopify_payments")
    assert slug == "shopify_payments"
    assert canonical == "shopify_payments"


def test_resolve_slug_komoju_variants():
    for g in (
        "KOMOJU - スマホ決済 (Smartphone Payments)",
        "KOMOJU - コンビニ決済 (Convenience Store)",
        "KOMOJU",
        "KOMOJU - クレカ",
    ):
        slug, canonical = resolve_partner_slug(g)
        assert slug == "komoju"
        assert canonical == "KOMOJU"


def test_resolve_slug_amazon_paypay():
    assert resolve_partner_slug("Amazon Pay") == ("amazon_pay", "Amazon Pay")
    assert resolve_partner_slug("PayPay") == ("paypay", "PayPay")


def test_resolve_slug_unknown_raises():
    with pytest.raises(UnknownGatewayError):
        resolve_partner_slug("Stripe")
    with pytest.raises(UnknownGatewayError):
        resolve_partner_slug("")


def test_resolve_uses_env(monkeypatch):
    monkeypatch.setenv(
        "SHOPIFY_SALES_PARTNER_SHOPIFY_PAYMENTS", "9999,Shopify Payments"
    )
    res = resolve("shopify_payments")
    assert res.partner_id == 9999
    assert res.partner_name == "Shopify Payments"
    assert res.canonical_gateway == "shopify_payments"


def test_resolve_komoju_env(monkeypatch):
    monkeypatch.setenv(
        "SHOPIFY_SALES_PARTNER_KOMOJU", "102026938,株式会社デジカ（KOMOJU）"
    )
    res = resolve("KOMOJU - スマホ決済 (Smartphone Payments)")
    assert res.partner_id == 102026938
    assert res.partner_name == "株式会社デジカ（KOMOJU）"
    assert res.canonical_gateway == "KOMOJU"


def test_resolve_partner_not_configured(monkeypatch):
    monkeypatch.delenv("SHOPIFY_SALES_PARTNER_SHOPIFY_PAYMENTS", raising=False)
    with pytest.raises(PartnerNotConfiguredError):
        resolve("shopify_payments")


def test_resolve_placeholder_value_treated_as_unset(monkeypatch):
    monkeypatch.setenv(
        "SHOPIFY_SALES_PARTNER_SHOPIFY_PAYMENTS", "__SHOPIFY_PAYMENTS_PARTNER_ID__"
    )
    with pytest.raises(PartnerNotConfiguredError):
        resolve("shopify_payments")


def test_resolve_invalid_format(monkeypatch):
    monkeypatch.setenv("SHOPIFY_SALES_PARTNER_PAYPAY", "no_comma_value")
    with pytest.raises(ValueError):
        resolve("PayPay")


def test_resolve_non_integer_id(monkeypatch):
    monkeypatch.setenv("SHOPIFY_SALES_PARTNER_PAYPAY", "abc,PayPay")
    with pytest.raises(ValueError):
        resolve("PayPay")
