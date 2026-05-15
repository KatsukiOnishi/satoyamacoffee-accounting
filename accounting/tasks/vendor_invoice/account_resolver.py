"""freee の過去取引から partner に対する最頻 account_item を推定する。

優先順位:
1. 過去90日の同 partner の deals で最頻の account_item_id（出現回数 >=1）
2. KNOWN_BANK_TRANSFER_VENDORS の default_account_item（名前 → ID 引き当て）
3. それも無ければ「外注加工費」をデフォルト
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta

from accounting.connectors.freee import FreeeClient
from accounting.core.logger import get_logger

logger = get_logger("vendor_invoice.account_resolver")

DEFAULT_FALLBACK_ACCOUNT_ITEM_NAME = "外注費"


@dataclass
class AccountResolution:
    account_item_id: int | None
    account_item_name: str | None
    source: str  # "past_deals" / "default_hint" / "fallback" / "none"
    needs_review: bool
    notes: str = ""


def _build_name_to_id(freee: FreeeClient) -> dict[str, int]:
    items = freee.get_account_items()
    out: dict[str, int] = {}
    for it in items:
        name = it.get("name")
        iid = it.get("id")
        if name and iid is not None:
            out[name] = int(iid)
    return out


def _resolve_by_name(
    name_to_id: dict[str, int], name: str
) -> tuple[int | None, str | None]:
    if not name:
        return None, None
    if name in name_to_id:
        return name_to_id[name], name
    # 軽い部分一致（"支払手数料" など完全名がない場合の保険）
    for n, i in name_to_id.items():
        if name in n or n in name:
            return i, n
    return None, None


def resolve_account_item(
    freee: FreeeClient,
    partner_id: int | None,
    default_hint_name: str | None,
    today: date | None = None,
) -> AccountResolution:
    today = today or date.today()
    name_to_id = _build_name_to_id(freee)

    # 1. 過去 90 日の同 partner deals から最頻
    if partner_id is not None:
        start = (today - timedelta(days=90)).isoformat()
        end = today.isoformat()
        try:
            deals = freee.list_deals(start_issue_date=start, end_issue_date=end)
        except Exception as e:
            logger.warning("account_resolver.list_deals_failed", error=str(e))
            deals = []
        counter: Counter[int] = Counter()
        for d in deals:
            if int(d.get("partner_id") or 0) != partner_id:
                continue
            for det in d.get("details", []) or []:
                aid = det.get("account_item_id")
                if aid is not None:
                    counter[int(aid)] += 1
        if counter:
            top_id, _ = counter.most_common(1)[0]
            name = next(
                (n for n, i in name_to_id.items() if i == top_id),
                None,
            )
            return AccountResolution(
                account_item_id=top_id,
                account_item_name=name,
                source="past_deals",
                needs_review=False,
                notes=f"top_count={counter[top_id]}",
            )

    # 2. KNOWN_BANK_TRANSFER_VENDORS の default
    if default_hint_name:
        aid, an = _resolve_by_name(name_to_id, default_hint_name)
        if aid is not None:
            return AccountResolution(
                account_item_id=aid,
                account_item_name=an,
                source="default_hint",
                needs_review=True,
                notes="no_past_deals_use_default",
            )

    # 3. フォールバック
    aid, an = _resolve_by_name(name_to_id, DEFAULT_FALLBACK_ACCOUNT_ITEM_NAME)
    if aid is not None:
        return AccountResolution(
            account_item_id=aid,
            account_item_name=an,
            source="fallback",
            needs_review=True,
            notes="fallback_to_default_outsourcing",
        )

    return AccountResolution(
        account_item_id=None,
        account_item_name=None,
        source="none",
        needs_review=True,
        notes="no_account_item_resolved",
    )


def find_account_item_id(freee: FreeeClient, name: str) -> int | None:
    """name → id の単純引き当て（CLI 等のヘルパー）。"""
    name_to_id = _build_name_to_id(freee)
    aid, _ = _resolve_by_name(name_to_id, name)
    return aid


def get_payables_account_item_id(freee: FreeeClient) -> int | None:
    """「未払金」勘定の account_item_id を返す。"""
    return find_account_item_id(freee, "未払金")


def get_unfiled_tax_code(_freee: FreeeClient | None = None) -> int:
    """デフォルト税区分（課税対応仕入10%）。

    freee の税区分IDは事業所共通固定値が多いので env で動的にしない。
    必要なら .env の FREEE_TAX_CODE_FEE で上書きできる（=0 だと未指定扱い）。
    """
    from accounting.config import settings

    tax = int(settings.freee_tax_code_fee or 0)
    if tax:
        return tax
    # 「課対仕入10%」相当の汎用コード。事業所によっては要調整。
    # freee の標準: 21=課税売上10%、136=課対仕入10% などのケースがある。
    return 136
