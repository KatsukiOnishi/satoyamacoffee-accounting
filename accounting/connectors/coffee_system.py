"""coffee_system (在庫管理) コネクタ。

責務: 月末棚卸資産スナップショット・ロット別/商品別原価の取得。

現状: `GET /api/inventory/value` で「現在時点の在庫評価額（JPY）」を取得する。
過去月の履歴は coffee_system 側に保存していないので、「月末当日に呼ぶ」運用前提。
"""
from __future__ import annotations

from typing import Any

import httpx

from accounting.config import settings
from accounting.core.logger import get_logger

logger = get_logger("coffee_system")


class CoffeeSystemClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not (base_url or settings.coffee_system_base_url):
            raise ValueError("COFFEE_SYSTEM_BASE_URL が未設定です")
        self.base_url = (base_url or settings.coffee_system_base_url).rstrip("/")
        self.api_key = api_key or settings.coffee_system_api_key
        self._client = httpx.Client(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CoffeeSystemClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def get_inventory_value(self) -> dict[str, Any]:
        """現時点の在庫評価額を取得する。

        coffee_system 側のレスポンス形式（想定）:
        ```json
        {
          "as_of": "2026-04-30",
          "total_jpy": 1234567,
          "breakdown": {
            "raw_bean_value": 1000000,
            "roasted_value": 234567
          }
        }
        ```

        Returns: 上記 dict。HTTP エラー時は httpx の例外を投げる。
        """
        url = f"{self.base_url}/api/inventory/value"
        res = self._client.get(url, headers=self._headers())
        if not res.is_success:
            logger.error(
                "coffee_system.inventory_value.api_error",
                status=res.status_code,
                response_body=res.text[:500],
            )
            res.raise_for_status()
        data = res.json()
        logger.info(
            "coffee_system.inventory_value.fetched",
            as_of=data.get("as_of"),
            total_jpy=data.get("total_jpy"),
        )
        return data
