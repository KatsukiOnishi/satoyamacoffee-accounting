"""coffee_system (在庫管理) コネクタ。後続タスクで実装する。

責務: 月末棚卸資産スナップショット・ロット別/商品別原価の取得。
"""
from __future__ import annotations

import httpx

from accounting.config import settings


class CoffeeSystemClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = (base_url or settings.coffee_system_base_url).rstrip("/")
        self.api_key = api_key or settings.coffee_system_api_key
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()
