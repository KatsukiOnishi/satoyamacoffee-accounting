"""Shopify コネクタ。後続タスクで実装する。

責務: EC 売上・決済手数料・送料収入の取得。
"""
from __future__ import annotations

import httpx

from accounting.config import settings


class ShopifyClient:
    def __init__(
        self,
        shop_domain: str | None = None,
        access_token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.shop_domain = shop_domain or settings.shopify_shop_domain
        self.access_token = access_token or settings.shopify_access_token
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()
