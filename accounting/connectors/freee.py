from __future__ import annotations

from typing import Any

import httpx

from accounting.config import settings
from accounting.core import idempotency
from accounting.core.dry_run import is_dry_run
from accounting.core.logger import get_logger

logger = get_logger("freee")

FREEE_API_BASE = "https://api.freee.co.jp"


class FreeeClient:
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
        self, payload: dict[str, Any], external_id: str, task: str, run_id: str
    ) -> dict[str, Any]:
        if idempotency.is_executed(task, external_id):
            existing = idempotency.get_execution(task, external_id) or {}
            logger.info(
                "freee.skip_duplicate",
                task=task,
                external_id=external_id,
                freee_journal_id=existing.get("freee_journal_id"),
            )
            return {"skipped": True, "reason": "already_executed", "record": existing}

        if is_dry_run():
            logger.info(
                "freee.register_journal.dry_run",
                task=task,
                external_id=external_id,
                payload=payload,
            )
            return {"skipped": False, "dry_run": True, "payload": payload}

        url = f"{self.base_url}/api/1/journals"
        body = {"company_id": int(self.company_id), **payload}
        try:
            res = self._client.post(url, json=body, headers=self._headers())
            res.raise_for_status()
            data = res.json()
            journal_id = str(data.get("journal", {}).get("id") or data.get("id") or "")
            idempotency.mark_executed(task, external_id, run_id, journal_id or None, "success")
            logger.info(
                "freee.register_journal.success",
                task=task,
                external_id=external_id,
                freee_journal_id=journal_id,
            )
            return {"skipped": False, "dry_run": False, "response": data}
        except Exception as e:
            idempotency.mark_executed(task, external_id, run_id, None, "failed")
            logger.error(
                "freee.register_journal.failed",
                task=task,
                external_id=external_id,
                error=str(e),
            )
            raise

    def register_invoice(
        self, payload: dict[str, Any], external_id: str, task: str, run_id: str
    ) -> dict[str, Any]:
        if idempotency.is_executed(task, external_id):
            existing = idempotency.get_execution(task, external_id) or {}
            logger.info(
                "freee.skip_duplicate",
                task=task,
                external_id=external_id,
                freee_invoice_id=existing.get("freee_journal_id"),
            )
            return {"skipped": True, "reason": "already_executed", "record": existing}

        if is_dry_run():
            logger.info(
                "freee.register_invoice.dry_run",
                task=task,
                external_id=external_id,
                payload=payload,
            )
            return {"skipped": False, "dry_run": True, "payload": payload}

        url = f"{self.base_url}/api/1/invoices"
        body = {"company_id": int(self.company_id), **payload}
        try:
            res = self._client.post(url, json=body, headers=self._headers())
            res.raise_for_status()
            data = res.json()
            invoice_id = str(data.get("invoice", {}).get("id") or data.get("id") or "")
            idempotency.mark_executed(task, external_id, run_id, invoice_id or None, "success")
            logger.info(
                "freee.register_invoice.success",
                task=task,
                external_id=external_id,
                freee_invoice_id=invoice_id,
            )
            return {"skipped": False, "dry_run": False, "response": data}
        except Exception as e:
            idempotency.mark_executed(task, external_id, run_id, None, "failed")
            logger.error(
                "freee.register_invoice.failed",
                task=task,
                external_id=external_id,
                error=str(e),
            )
            raise

    def get_account_items(self) -> list[dict[str, Any]]:
        url = f"{self.base_url}/api/1/account_items"
        params = {"company_id": self.company_id}
        res = self._client.get(url, params=params, headers=self._headers())
        res.raise_for_status()
        data = res.json()
        return data.get("account_items", [])
