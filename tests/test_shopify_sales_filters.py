from __future__ import annotations

from accounting.tasks.shopify_sales.filters import should_exclude


def _order(**overrides):
    base = {
        "name": "#1001",
        "tags": [],
        "cancelledAt": None,
        "displayFinancialStatus": "PAID",
        "paymentGatewayNames": ["shopify_payments"],
    }
    base.update(overrides)
    return base


def test_paid_is_included():
    skip, reason = should_exclude(_order())
    assert skip is False
    assert reason is None


def test_partially_refunded_is_included():
    skip, _ = should_exclude(_order(displayFinancialStatus="PARTIALLY_REFUNDED"))
    assert skip is False


def test_exclude_tag_filters():
    skip, reason = should_exclude(_order(tags=["exclude-from-accounting"]))
    assert skip is True
    assert "exclude-from-accounting" in reason


def test_exclude_tag_as_csv_string_filters():
    # Shopify REST 由来だと CSV 文字列で来る場合あり
    skip, reason = should_exclude(_order(tags="something, exclude-from-accounting , other"))
    assert skip is True


def test_cancelled_filters():
    skip, reason = should_exclude(_order(cancelledAt="2026-04-15T12:00:00Z"))
    assert skip is True
    assert reason == "cancelled"


def test_pending_status_filters():
    skip, reason = should_exclude(_order(displayFinancialStatus="PENDING"))
    assert skip is True
    assert "PENDING" in reason


def test_refunded_status_filters():
    # 完全返金は除外（部分返金とは別扱い）
    skip, _ = should_exclude(_order(displayFinancialStatus="REFUNDED"))
    assert skip is True
