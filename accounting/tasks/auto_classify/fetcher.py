"""auto-classify 用の取得層 + freee マスタ（勘定科目・税区分・取引先）取得。

ar-reconcile の fetcher と重複する関数はあるが、責務分離のため別ファイルで持つ。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from accounting.connectors.freee import FreeeClient
from accounting.core.logger import get_logger
from accounting.tasks.ar_reconcile import fetcher as ar_fetcher
from accounting.tasks.auto_classify.models import WalletTxnForClassify

log = get_logger("auto_classify.fetcher")


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def fetch_unreconciled_wallet_txns(
    freee: FreeeClient,
    *,
    start_date: date,
    end_date: date,
) -> list[WalletTxnForClassify]:
    """期間内の未紐付 wallet_txn（入金/出金 両方）を WalletTxnForClassify として返す。"""
    raw = ar_fetcher.fetch_unreconciled_wallet_txns_all(
        freee, start_date=start_date, end_date=end_date
    )
    out: list[WalletTxnForClassify] = []
    for t in raw:
        d_ = _parse_date(t.get("date"))
        if d_ is None:
            continue
        out.append(
            WalletTxnForClassify(
                id=int(t["id"]),
                date=d_,
                description=t.get("description") or "",
                amount=int(t.get("amount") or 0),
                walletable_type=t.get("walletable_type"),
                walletable_id=t.get("walletable_id"),
                walletable_name=t.get("_walletable_name"),
                entry_side=t.get("entry_side"),
            )
        )
    return out


def fetch_freee_masters(
    freee: FreeeClient,
) -> dict[str, Any]:
    """勘定科目 / 税区分 / 取引先 のマスタを一括取得して dict で返す。

    Returns:
      {
        'account_items': list[dict],          # freee.get_account_items() の結果
        'account_items_by_name': dict[str, dict],
        'partners': list[dict],
        'partners_by_norm_name': dict[str, dict],  # 正規化キー→partner
        'tax_codes_by_name': dict[str, dict],
      }
    """
    from accounting.tasks.ar_reconcile.matcher import normalize_partner_name

    account_items = freee.get_account_items()
    partners = freee.list_partners()
    # 税区分は freee.list_walletables 同様の薄い endpoint が無いため、
    # account_items の available_tax_names 等から構築するのではなく、
    # ここでは「Claude が返した tax_code_name 文字列を最終出力にそのまま使う」方針。
    # freee の Deal payload は tax_code（数値）必須なので、API 経由で取得する必要がある。
    tax_codes_by_name = _fetch_tax_codes(freee)

    return {
        "account_items": account_items,
        "account_items_by_name": {
            (a.get("name") or "").strip(): a for a in account_items
        },
        "partners": partners,
        "partners_by_norm_name": {
            normalize_partner_name(p.get("name")): p
            for p in partners
            if p.get("name")
        },
        "tax_codes_by_name": tax_codes_by_name,
    }


def _fetch_tax_codes(freee: FreeeClient) -> dict[str, dict[str, Any]]:
    """freee の税区分マスタを `{name: {id, name, ...}}` で返す。

    freee API: GET /api/1/taxes/codes
    """
    url = f"{freee.base_url}/api/1/taxes/codes"
    # company_id は不要だが付けても問題ない
    params: dict[str, Any] = {"company_id": freee.company_id} if freee.company_id else {}
    res = freee._request("GET", url, params=params)
    if not res.is_success:
        log.warning(
            "auto_classify.tax_codes_unavailable",
            status=res.status_code,
            body=res.text[:300],
        )
        return {}
    data = res.json()
    items = data.get("taxes") or data.get("tax_codes") or []
    out: dict[str, dict[str, Any]] = {}
    for it in items:
        name = (it.get("name_ja") or it.get("name") or "").strip()
        if name:
            out[name] = it
    return out


def fetch_past_examples(
    freee: FreeeClient,
    *,
    days_back: int = 90,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """過去 90 日分の確定済み expense / income 取引から few-shot 用サンプルを返す。

    Claude のプロンプトに混ぜる軽量サンプル。期間内全件のうち先頭 `limit` 件のみ。
    """
    from datetime import timedelta

    end = date.today()
    start = end - timedelta(days=days_back)
    deals = freee.list_deals(
        start_issue_date=start.isoformat(),
        end_issue_date=end.isoformat(),
    )
    examples: list[dict[str, Any]] = []
    for d in deals[:limit]:
        details = d.get("details") or []
        if not details:
            continue
        d0 = details[0]
        examples.append(
            {
                "type": d.get("type"),
                "date": d.get("issue_date"),
                "partner_name": d.get("partner_name") or "",
                "amount": d.get("amount"),
                "description": d0.get("description") or "",
                "account_item_id": d0.get("account_item_id"),
                "account_item_name": d0.get("account_item_name") or "",
                "tax_name": d0.get("tax_name") or "",
            }
        )
    return examples
