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

    認証: `accounting.core.freee_auth.get_access_token()` 経由で動的にトークン取得。
    期限切れ間近なら自動で refresh される。さらに 401 を受けたら一度だけ
    force_refresh して retry する（保険）。

    後方互換: secrets/freee_tokens.json 不在（bootstrap 未完了）の場合は、
    `settings.freee_api_key` を使う旧挙動にフォールバック。
    """

    def __init__(
        self,
        api_key: str | None = None,
        company_id: str | None = None,
        base_url: str = FREEE_API_BASE,
        timeout: float = 30.0,
    ) -> None:
        # 明示的に渡された api_key があればそれを使う（テストでの上書き等）。
        # None なら OAuthManager → settings.freee_api_key の順で動的解決。
        self._explicit_api_key = api_key
        self.company_id = company_id or settings.freee_company_id
        self.base_url = base_url
        self._client = httpx.Client(timeout=timeout)

    @property
    def api_key(self) -> str:
        """現在の access_token を返す（後方互換用プロパティ）。

        通常は `_headers()` が直接 get_access_token を呼ぶので、これに依存しない。
        """
        return self._resolve_access_token()

    def _resolve_access_token(self) -> str:
        if self._explicit_api_key is not None:
            return self._explicit_api_key
        # 動的解決
        from accounting.core.freee_auth import (
            FreeeBootstrapRequiredError,
            get_access_token,
        )

        try:
            return get_access_token()
        except FreeeBootstrapRequiredError:
            # bootstrap 未完了: settings.freee_api_key にフォールバック
            return settings.freee_api_key

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._resolve_access_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Api-Version": "2020-06-15",
        }

    def _request(
        self, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        """全 freee API 呼び出しの共通エントリポイント。

        - 都度 `_headers()` を構築 → 最新の access_token を反映
        - 401 を受けたら一度だけ force_refresh して retry（保険）
        - 二度目も 401 ならそのまま返す（呼び出し側で raise_for_status）
        """
        resp = self._client.request(method, url, headers=self._headers(), **kwargs)
        if resp.status_code == 401 and self._explicit_api_key is None:
            from accounting.core.freee_auth import (
                FreeeAuthError,
                FreeeBootstrapRequiredError,
                FreeeRefreshTokenInvalidError,
                build_authorize_url,
                force_refresh,
            )

            logger.warning(
                "freee.unauthorized_retry",
                url=url,
                method=method,
            )
            try:
                force_refresh()
            except FreeeBootstrapRequiredError:
                # bootstrap してないのに 401 → そもそも api_key が空 or 不正
                return resp
            except FreeeRefreshTokenInvalidError as e:
                # refresh_token が完全に失効。Resend で再認可案内 → 例外伝播。
                logger.error("freee.refresh_token_invalid", error=str(e))
                try:
                    from accounting.core.notifier import notify_refresh_token_invalid

                    notify_refresh_token_invalid(reauth_url=build_authorize_url())
                except Exception as notif_err:
                    logger.error("freee.notify_failed", error=str(notif_err))
                raise
            except FreeeAuthError as e:
                # ネットワークエラーや設定不足。呼び出し側で notify_failure 等が走る前提。
                logger.error("freee.refresh_failed_on_401", error=str(e))
                raise
            resp = self._client.request(method, url, headers=self._headers(), **kwargs)
        return resp

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
        resp = self._request("POST", url, json=body)
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
        resp = self._request("POST", url, json=body)
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
        res = self._request("GET", url, params=params)
        res.raise_for_status()
        return res.json().get("account_items", [])

    # ---- 月次決算ハブが必要とする読み取り系 / user_matchers 系 ----

    def list_deals(
        self,
        *,
        start_issue_date: str,
        end_issue_date: str,
        page_size: int = 100,
        deal_type: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """確定済み取引を期間指定で全件取得（自動ページング）。

        Args:
            start_issue_date / end_issue_date: YYYY-MM-DD
            page_size: 1ページの件数（freee 上限 100）
            deal_type: 'income' or 'expense' で絞り込む（None なら全種）
            status: 'settled' or 'unsettled' で絞り込む（None なら全状態）

        Returns: 全 deals のフラットリスト（details 込み）
        """
        url = f"{self.base_url}/api/1/deals"
        all_deals: list[dict[str, Any]] = []
        offset = 0
        while True:
            params: dict[str, Any] = {
                "company_id": self.company_id,
                "start_issue_date": start_issue_date,
                "end_issue_date": end_issue_date,
                "limit": page_size,
                "offset": offset,
                "accruals": "with",
            }
            if deal_type:
                params["type"] = deal_type
            if status:
                params["status"] = status
            res = self._request("GET", url, params=params)
            res.raise_for_status()
            chunk = res.json().get("deals", [])
            all_deals.extend(chunk)
            logger.info(
                "freee.list_deals.page",
                offset=offset,
                returned=len(chunk),
                total=len(all_deals),
                deal_type=deal_type,
                status=status,
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
            res = self._request("GET", url, params=params)
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
        res = self._request("GET", url, params=params)
        res.raise_for_status()
        return res.json().get("walletables", [])

    def list_wallet_txns(
        self,
        *,
        walletable_type: str | None = None,
        walletable_id: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        entry_side: str | None = None,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """口座明細を取得（ページング自動処理）。

        walletable_type と walletable_id は同時指定が必須（freee 仕様）。
        指定しない場合は事業所全口座を対象とする。
        `entry_side` は 'income' / 'expense' で wallet_txn を絞り込む。
        freee API も同 param を受けるが、後方互換のためクライアント側でも再フィルタする。
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
            if entry_side:
                params["entry_side"] = entry_side
            res = self._request("GET", url, params=params)
            res.raise_for_status()
            chunk = res.json().get("wallet_txns", [])
            all_txns.extend(chunk)
            if len(chunk) < page_size:
                break
            offset += page_size
        if entry_side:
            # クライアント側でも保険として再フィルタ
            all_txns = [t for t in all_txns if t.get("entry_side") == entry_side]
        return all_txns

    def create_payment_for_deal(
        self,
        *,
        deal_id: int,
        payment_date: str,
        from_walletable_type: str,
        from_walletable_id: int,
        amount: int,
        external_id: str,
        task: str,
    ) -> dict[str, Any]:
        """未決済取引に支払いを追加して消し込む。

        freee API: POST /api/1/deals/{id}/payments
        Body: { date, from_walletable_type, from_walletable_id, amount, company_id }

        dry-run なら payload ログ出力のみ。
        """
        if is_dry_run():
            logger.info(
                "freee.payment.dry_run",
                task=task,
                external_id=external_id,
                deal_id=deal_id,
                payment_date=payment_date,
                from_walletable_type=from_walletable_type,
                from_walletable_id=from_walletable_id,
                amount=amount,
            )
            return {"dry_run": True, "external_id": external_id}

        url = f"{self.base_url}/api/1/deals/{int(deal_id)}/payments"
        body = {
            "company_id": int(self.company_id) if self.company_id else 0,
            "date": payment_date,
            "from_walletable_type": from_walletable_type,
            "from_walletable_id": int(from_walletable_id),
            "amount": int(amount),
        }
        res = self._request("POST", url, json=body)
        if not res.is_success:
            logger.error(
                "freee.payment.api_error",
                task=task,
                external_id=external_id,
                deal_id=deal_id,
                status=res.status_code,
                response_body=res.text[:1000],
                payload=body,
            )
            res.raise_for_status()
        data = res.json()
        payment_id = (data.get("payment") or {}).get("id") or data.get("id")
        logger.info(
            "freee.payment.created",
            task=task,
            external_id=external_id,
            deal_id=deal_id,
            payment_id=payment_id,
        )
        return {"payment_id": payment_id, "raw": data, "external_id": external_id}

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
            res = self._request("GET", url, params=params)
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
        res = self._request("POST", url, params=params, json=payload)
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
        res = self._request("PUT", url, params=params, json=payload)
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
        res = self._request("DELETE", url, params=params)
        res.raise_for_status()
        logger.info("freee.user_matcher.deleted", matcher_id=matcher_id)

    # ---- Deals（取引）作成 ----

    def create_deal(
        self, payload: dict[str, Any], external_id: str, task: str
    ) -> dict[str, Any]:
        """取引（Deal）を作成する。dry-run なら payload ログ出力のみ。

        freee API: POST /api/1/deals
        Body はラップせず flat に payload を送る（freee 仕様）。

        Args:
            payload: deal 本体（issue_date / type / company_id / partner_id / details ほか）
            external_id: 冪等性管理用 ID（呼び出し側で executed_operations にマーク）
            task: ロギング用のタスク名
        """
        if is_dry_run():
            logger.info(
                "freee.deal.dry_run",
                task=task,
                external_id=external_id,
                payload=payload,
            )
            return {"dry_run": True, "external_id": external_id}

        url = f"{self.base_url}/api/1/deals"
        res = self._request("POST", url, json=payload)
        if not res.is_success:
            logger.error(
                "freee.deal.api_error",
                task=task,
                external_id=external_id,
                status=res.status_code,
                response_body=res.text[:1000],
                payload=payload,
            )
            res.raise_for_status()
        data = res.json()
        deal_id = (data.get("deal") or {}).get("id") or data.get("id")
        logger.info(
            "freee.deal.created",
            task=task,
            external_id=external_id,
            deal_id=deal_id,
        )
        return {"deal_id": deal_id, "raw": data, "external_id": external_id}

    # ---- 振替伝票（manual_journals）作成 ----

    def create_manual_journal(
        self, payload: dict[str, Any], external_id: str, task: str
    ) -> dict[str, Any]:
        """振替伝票（manual_journal）を作成する。dry-run なら payload ログ出力のみ。

        freee API: POST /api/1/manual_journals
        Body はラップせず flat に payload を送る（freee 仕様、create_deal と同じ）。
        旧コードは `{"manual_journal": payload}` で wrap していたが、freee 側で
        `"company_id, details, issue_date が指定されていません"` の 400 を返すバグの
        原因となっていた。

        Args:
            payload: manual_journal 本体（issue_date / company_id / details ほか）
            external_id: 冪等性管理用 ID（呼び出し側で executed_operations にマーク）
            task: ロギング用のタスク名
        """
        if is_dry_run():
            logger.info(
                "freee.manual_journal.dry_run",
                task=task,
                external_id=external_id,
                payload=payload,
            )
            return {"dry_run": True, "external_id": external_id}

        url = f"{self.base_url}/api/1/manual_journals"
        res = self._request("POST", url, json=payload)
        if not res.is_success:
            logger.error(
                "freee.manual_journal.api_error",
                task=task,
                external_id=external_id,
                status=res.status_code,
                response_body=res.text,
                payload=payload,
            )
            res.raise_for_status()
        data = res.json()
        manual_journal_id = (data.get("manual_journal") or {}).get("id") or data.get("id")
        logger.info(
            "freee.manual_journal.created",
            task=task,
            external_id=external_id,
            manual_journal_id=manual_journal_id,
        )
        return {
            "manual_journal_id": manual_journal_id,
            "raw": data,
            "external_id": external_id,
        }

    def delete_manual_journal(self, manual_journal_id: int) -> None:
        """振替伝票を ID 指定で削除する。dry-run では実 API を叩かない。"""
        if is_dry_run():
            logger.info(
                "freee.manual_journal.delete_dry_run",
                manual_journal_id=manual_journal_id,
            )
            return
        url = f"{self.base_url}/api/1/manual_journals/{manual_journal_id}"
        params = {"company_id": self.company_id}
        res = self._request("DELETE", url, params=params)
        if not res.is_success:
            logger.error(
                "freee.manual_journal.delete_api_error",
                manual_journal_id=manual_journal_id,
                status=res.status_code,
                body=res.text[:500],
            )
            res.raise_for_status()
        logger.info("freee.manual_journal.deleted", manual_journal_id=manual_journal_id)
