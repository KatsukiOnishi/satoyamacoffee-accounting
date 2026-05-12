from __future__ import annotations

from typing import Any

import httpx

from accounting.config import settings
from accounting.core.dry_run import is_dry_run
from accounting.core.logger import get_logger

FREEE_API_BASE = "https://api.freee.co.jp"

logger = get_logger("freee")


class FreeeClient:
    """freee API クライアント（薄いラッパー）。

    冪等性チェック（is_executed / mark_executed）は呼び出し側のタスクで管理する。
    本クラスは「実APIコール + dry-run のスキップ + レスポンス返却」に専念する。
    """

    def __init__(
        self,
        api_key: str | None = None,
        company_id: str | None = None,
        base_url: str = FREEE_API_BASE,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key or settings.freee_api_key
        self.company_id = company_id or settings.freee_company_id
        self.base_url = base_url
        self._client = httpx.Client(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Api-Version": "2020-06-15",
        }

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FreeeClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def register_journal(
        self, payload: dict[str, Any], external_id: str, task: str
    ) -> dict[str, Any]:
        if is_dry_run():
            logger.info(
                "freee.dry_run",
                task=task,
                external_id=external_id,
                payload=payload,
            )
            return {"dry_run": True, "external_id": external_id}

        url = f"{self.base_url}/api/1/journals"
        body = {"journal": payload}
        resp = self._client.post(url, json=body, headers=self._headers())
        resp.raise_for_status()
        data = resp.json()
        journal_id = data.get("journal", {}).get("id")
        logger.info(
            "freee.journal_registered",
            task=task,
            external_id=external_id,
            journal_id=journal_id,
        )
        return {"journal_id": journal_id, "raw": data, "external_id": external_id}

    def register_invoice(
        self, payload: dict[str, Any], external_id: str, task: str
    ) -> dict[str, Any]:
        if is_dry_run():
            logger.info(
                "freee.dry_run_invoice",
                task=task,
                external_id=external_id,
                payload=payload,
            )
            return {"dry_run": True, "external_id": external_id}

        url = f"{self.base_url}/api/1/invoices"
        body = {"invoice": payload}
        resp = self._client.post(url, json=body, headers=self._headers())
        resp.raise_for_status()
        data = resp.json()
        invoice_id = data.get("invoice", {}).get("id")
        logger.info(
            "freee.invoice_registered",
            task=task,
            external_id=external_id,
            invoice_id=invoice_id,
        )
        return {"invoice_id": invoice_id, "raw": data, "external_id": external_id}

    def get_account_items(self) -> list[dict[str, Any]]:
        url = f"{self.base_url}/api/1/account_items"
        params = {"company_id": self.company_id}
        res = self._client.get(url, params=params, headers=self._headers())
        res.raise_for_status()
        return res.json().get("account_items", [])
