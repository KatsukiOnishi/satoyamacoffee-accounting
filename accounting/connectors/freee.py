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

    # ---- 月次決算ハブが必要とする読み取り系 / user_matchers 系 ----

    def list_deals(
        self,
        *,
        start_issue_date: str,
        end_issue_date: str,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """確定済み取引を期間指定で全件取得（自動ページング）。

        Args:
            start_issue_date / end_issue_date: YYYY-MM-DD
            page_size: 1ページの件数（freee 上限 100）

        Returns: 全 deals のフラットリスト（details 込み）
        """
        url = f"{self.base_url}/api/1/deals"
        all_deals: list[dict[str, Any]] = []
        offset = 0
        while True:
            params = {
                "company_id": self.company_id,
                "start_issue_date": start_issue_date,
                "end_issue_date": end_issue_date,
                "limit": page_size,
                "offset": offset,
                "accruals": "with",
            }
            res = self._client.get(url, params=params, headers=self._headers())
            res.raise_for_status()
            chunk = res.json().get("deals", [])
            all_deals.extend(chunk)
            logger.info(
                "freee.list_deals.page",
                offset=offset,
                returned=len(chunk),
                total=len(all_deals),
            )
            if len(chunk) < page_size:
                break
            offset += page_size
        return all_deals

    def list_partners(
        self,
        *,
        page_size: int = 100,
        keyword: str | None = None,
    ) -> list[dict[str, Any]]:
        """取引先一覧（自動ページング）。"""
        url = f"{self.base_url}/api/1/partners"
        all_partners: list[dict[str, Any]] = []
        offset = 0
        while True:
            params: dict[str, Any] = {
                "company_id": self.company_id,
                "limit": page_size,
                "offset": offset,
            }
            if keyword:
                params["keyword"] = keyword
            res = self._client.get(url, params=params, headers=self._headers())
            res.raise_for_status()
            chunk = res.json().get("partners", [])
            all_partners.extend(chunk)
            if len(chunk) < page_size:
                break
            offset += page_size
        return all_partners

    def list_walletables(self, *, walletable_type: str | None = None) -> list[dict[str, Any]]:
        """口座一覧（銀行/クレジットカード/その他）。"""
        url = f"{self.base_url}/api/1/walletables"
        params: dict[str, Any] = {"company_id": self.company_id}
        if walletable_type:
            params["type"] = walletable_type
        res = self._client.get(url, params=params, headers=self._headers())
        res.raise_for_status()
        return res.json().get("walletables", [])

    def list_wallet_txns(
        self,
        *,
        walletable_type: str | None = None,
        walletable_id: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """口座明細を取得（ページング自動処理）。

        walletable_type と walletable_id は同時指定が必須（freee 仕様）。
        指定しない場合は事業所全口座を対象とする。
        """
        url = f"{self.base_url}/api/1/wallet_txns"
        all_txns: list[dict[str, Any]] = []
        offset = 0
        while True:
            params: dict[str, Any] = {
                "company_id": self.company_id,
                "limit": page_size,
                "offset": offset,
            }
            if walletable_type and walletable_id is not None:
                params["walletable_type"] = walletable_type
                params["walletable_id"] = walletable_id
            if start_date:
                params["start_date"] = start_date
            if end_date:
                params["end_date"] = end_date
            res = self._client.get(url, params=params, headers=self._headers())
            res.raise_for_status()
            chunk = res.json().get("wallet_txns", [])
            all_txns.extend(chunk)
            if len(chunk) < page_size:
                break
            offset += page_size
        return all_txns

    def list_user_matchers(self, *, page_size: int = 100) -> list[dict[str, Any]]:
        """自動仕訳ルール一覧（自動ページング、全 act 対象）。"""
        url = f"{self.base_url}/api/1/user_matchers"
        all_items: list[dict[str, Any]] = []
        offset = 0
        while True:
            params = {
                "company_id": self.company_id,
                "limit": page_size,
                "offset": offset,
            }
            res = self._client.get(url, params=params, headers=self._headers())
            res.raise_for_status()
            chunk = res.json().get("data", [])
            all_items.extend(chunk)
            if len(chunk) < page_size:
                break
            offset += page_size
        return all_items

    def create_user_matcher(
        self, payload: dict[str, Any], external_id: str, task: str
    ) -> dict[str, Any]:
        """自動仕訳ルールを作成する。dry-run なら payload ログ出力のみ。"""
        if is_dry_run():
            logger.info(
                "freee.user_matcher.dry_run",
                task=task,
                external_id=external_id,
                payload=payload,
            )
            return {"dry_run": True, "external_id": external_id}

        url = f"{self.base_url}/api/1/user_matchers"
        params = {"company_id": self.company_id}
        res = self._client.post(url, params=params, json=payload, headers=self._headers())
        if not res.is_success:
            logger.error(
                "freee.user_matcher.api_error",
                task=task,
                external_id=external_id,
                status=res.status_code,
                response_body=res.text,
                payload=payload,
            )
            res.raise_for_status()
        data = res.json()
        matcher_id = data.get("id") or data.get("user_matcher", {}).get("id")
        logger.info(
            "freee.user_matcher.created",
            task=task,
            external_id=external_id,
            matcher_id=matcher_id,
        )
        return {"id": matcher_id, "raw": data, "external_id": external_id}

    def update_user_matcher(
        self, matcher_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """既存の自動仕訳ルールを更新する。dry-run なら payload ログ出力のみ。

        freee API 仕様により、PUT 時は act/active/condition/description/entry_side_str/
        priority/account_item_name/tax_name など必須フィールドを再送する必要がある。
        呼び出し側で既存値とマージした payload を渡すこと。
        """
        if is_dry_run():
            logger.info(
                "freee.user_matcher.update_dry_run",
                matcher_id=matcher_id,
                payload=payload,
            )
            return {"dry_run": True, "matcher_id": matcher_id}

        url = f"{self.base_url}/api/1/user_matchers/{matcher_id}"
        params = {"company_id": self.company_id}
        res = self._client.put(url, params=params, json=payload, headers=self._headers())
        if not res.is_success:
            logger.error(
                "freee.user_matcher.update_api_error",
                matcher_id=matcher_id,
                status=res.status_code,
                response_body=res.text,
                payload=payload,
            )
            res.raise_for_status()
        data = res.json()
        logger.info("freee.user_matcher.updated", matcher_id=matcher_id)
        return {"id": matcher_id, "raw": data}

    def delete_user_matcher(self, matcher_id: int) -> None:
        """自動仕訳ルールを削除する。dry-run でも実 API は叩かない。"""
        if is_dry_run():
            logger.info("freee.user_matcher.delete_dry_run", matcher_id=matcher_id)
            return
        url = f"{self.base_url}/api/1/user_matchers/{matcher_id}"
        params = {"company_id": self.company_id}
        res = self._client.delete(url, params=params, headers=self._headers())
        res.raise_for_status()
        logger.info("freee.user_matcher.deleted", matcher_id=matcher_id)
