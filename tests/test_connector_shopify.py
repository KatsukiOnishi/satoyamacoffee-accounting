from __future__ import annotations

import pytest

from accounting.connectors.shopify import (
    DEFAULT_API_VERSION,
    ShopifyClient,
    ShopifyError,
    jst_month_range_utc,
)


def test_jst_month_range_april_2026():
    """JST 2026-04 → UTC 2026-03-31T15:00:00Z 〜 2026-04-30T15:00:00Z"""
    start, end = jst_month_range_utc(2026, 4)
    assert start == "2026-03-31T15:00:00Z"
    assert end == "2026-04-30T15:00:00Z"


def test_jst_month_range_december_wraps_year():
    start, end = jst_month_range_utc(2026, 12)
    assert start == "2026-11-30T15:00:00Z"
    assert end == "2026-12-31T15:00:00Z"


def test_jst_month_range_january():
    start, end = jst_month_range_utc(2027, 1)
    # JST 2027-01-01 00:00 → UTC 2026-12-31 15:00
    assert start == "2026-12-31T15:00:00Z"
    assert end == "2027-01-31T15:00:00Z"


def test_jst_month_range_invalid_month():
    with pytest.raises(ValueError):
        jst_month_range_utc(2026, 0)
    with pytest.raises(ValueError):
        jst_month_range_utc(2026, 13)


def test_shopify_client_requires_domain(monkeypatch):
    monkeypatch.setenv("SHOPIFY_SHOP_DOMAIN", "")
    with pytest.raises(ShopifyError):
        ShopifyClient(access_token="dummy")


def test_shopify_client_requires_token(monkeypatch):
    monkeypatch.delenv("SHOPIFY_ADMIN_API_TOKEN", raising=False)
    monkeypatch.delenv("SHOPIFY_ACCESS_TOKEN", raising=False)
    with pytest.raises(ShopifyError):
        ShopifyClient(shop_domain="x.myshopify.com")


def test_shopify_client_default_api_version(monkeypatch):
    monkeypatch.delenv("SHOPIFY_API_VERSION", raising=False)
    c = ShopifyClient(shop_domain="x.myshopify.com", access_token="t")
    assert c.api_version == DEFAULT_API_VERSION
    c.close()


def test_shopify_client_api_version_override():
    c = ShopifyClient(
        shop_domain="x.myshopify.com", access_token="t", api_version="2025-10"
    )
    assert c.endpoint == "https://x.myshopify.com/admin/api/2025-10/graphql.json"
    c.close()


def test_iter_orders_paginates(monkeypatch):
    """カーソルベース paginate のロジック検証（_post をスタブ）。"""
    client = ShopifyClient(
        shop_domain="x.myshopify.com", access_token="t", api_version="2026-01"
    )

    pages = [
        {
            "data": {
                "orders": {
                    "edges": [
                        {"cursor": "c1", "node": {"id": "1"}},
                        {"cursor": "c2", "node": {"id": "2"}},
                    ],
                    "pageInfo": {"hasNextPage": True, "endCursor": "c2"},
                }
            }
        },
        {
            "data": {
                "orders": {
                    "edges": [
                        {"cursor": "c3", "node": {"id": "3"}},
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": "c3"},
                }
            }
        },
    ]
    calls: list[dict] = []

    def fake_post(payload):
        calls.append(payload)
        return pages[len(calls) - 1]

    monkeypatch.setattr(client, "_post", fake_post)

    result = list(client.iter_orders(query="created_at:>=2026-04-01", page_size=2))
    client.close()

    assert [o["id"] for o in result] == ["1", "2", "3"]
    assert calls[0]["variables"]["cursor"] is None
    assert calls[1]["variables"]["cursor"] == "c2"
    assert calls[0]["variables"]["first"] == 2
