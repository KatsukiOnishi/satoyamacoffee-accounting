"""Shopify Admin GraphQL コネクタ。

責務: shop の Orders を JST 月境界で取得し、生 JSON のリストを返す。
集計・partner マッピング・freee 登録は呼び出し側（tasks/shopify_sales）の責務。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import httpx

from accounting.config import settings
from accounting.core.logger import get_logger

logger = get_logger("shopify")

DEFAULT_API_VERSION = "2026-01"
DEFAULT_PAGE_SIZE = 50

ORDERS_QUERY = """
query GetOrders($cursor: String, $query: String!, $first: Int!) {
  orders(first: $first, after: $cursor, query: $query, sortKey: CREATED_AT) {
    edges {
      cursor
      node {
        id
        name
        createdAt
        tags
        cancelledAt
        displayFinancialStatus
        paymentGatewayNames
        totalPriceSet { shopMoney { amount currencyCode } }
        subtotalPriceSet { shopMoney { amount } }
        totalShippingPriceSet { shopMoney { amount } }
        totalTaxSet { shopMoney { amount } }
        totalDiscountsSet { shopMoney { amount } }
        totalRefundedSet { shopMoney { amount } }
        transactions {
          gateway
          kind
          status
          amountSet { shopMoney { amount } }
          fees {
            amount { amount }
            rate
            type
            flatFeeName
          }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
""".strip()


class ShopifyError(Exception):
    """Shopify Admin API 呼び出しの基底エラー。"""


class ShopifyClient:
    def __init__(
        self,
        shop_domain: str | None = None,
        access_token: str | None = None,
        api_version: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        # 正規キーは SHOPIFY_ADMIN_API_TOKEN（仕様書 §4-1）。後方互換で
        # 既存 .env の SHOPIFY_ACCESS_TOKEN もフォールバックとして拾う。
        self.shop_domain = shop_domain or settings.shopify_shop_domain
        self.access_token = (
            access_token
            or os.environ.get("SHOPIFY_ADMIN_API_TOKEN", "").strip()
            or settings.shopify_access_token
        )
        self.api_version = (
            api_version
            or os.environ.get("SHOPIFY_API_VERSION", "").strip()
            or DEFAULT_API_VERSION
        )
        if not self.shop_domain:
            raise ShopifyError("SHOPIFY_SHOP_DOMAIN が未設定です（.env を確認）")
        if not self.access_token:
            raise ShopifyError(
                "SHOPIFY_ADMIN_API_TOKEN が未設定です（.env を確認）"
            )
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ShopifyClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @property
    def endpoint(self) -> str:
        return f"https://{self.shop_domain}/admin/api/{self.api_version}/graphql.json"

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "X-Shopify-Access-Token": self.access_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        res = self._client.post(self.endpoint, headers=headers, json=payload)
        if res.status_code != 200:
            raise ShopifyError(
                f"Shopify API status={res.status_code} body={res.text[:500]}"
            )
        data = res.json()
        if "errors" in data:
            raise ShopifyError(f"Shopify GraphQL errors: {data['errors']}")
        return data

    def iter_orders(
        self,
        *,
        query: str,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Iterator[dict[str, Any]]:
        """Orders をカーソルベースで全件 yield する。"""
        cursor: str | None = None
        page = 0
        while True:
            page += 1
            payload = {
                "query": ORDERS_QUERY,
                "variables": {"cursor": cursor, "query": query, "first": page_size},
            }
            data = self._post(payload)
            orders = data.get("data", {}).get("orders", {})
            edges = orders.get("edges", [])
            logger.info(
                "shopify.fetch.page",
                page=page,
                returned=len(edges),
                cursor=cursor,
            )
            for edge in edges:
                yield edge["node"]
            page_info = orders.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break

    def list_orders_for_jst_month(
        self,
        *,
        year: int,
        month: int,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> list[dict[str, Any]]:
        start_utc, end_utc = jst_month_range_utc(year, month)
        query = f"created_at:>={start_utc} created_at:<{end_utc}"
        logger.info(
            "shopify.fetch.start",
            year=year,
            month=month,
            start_utc=start_utc,
            end_utc=end_utc,
        )
        orders = list(self.iter_orders(query=query, page_size=page_size))
        logger.info(
            "shopify.fetch.complete", year=year, month=month, total=len(orders)
        )
        return orders


def jst_month_range_utc(year: int, month: int) -> tuple[str, str]:
    """YYYY-MM の JST 月範囲を UTC ISO 8601 形式で返す。

    例: jst_month_range_utc(2026, 4)
        -> ("2026-03-31T15:00:00Z", "2026-04-30T15:00:00Z")
    """
    if not (1 <= month <= 12):
        raise ValueError(f"month は 1-12: {month}")
    jst = timezone(timedelta(hours=9))
    start_jst = datetime(year, month, 1, 0, 0, 0, tzinfo=jst)
    if month == 12:
        end_jst = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=jst)
    else:
        end_jst = datetime(year, month + 1, 1, 0, 0, 0, tzinfo=jst)
    return (
        start_jst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_jst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
