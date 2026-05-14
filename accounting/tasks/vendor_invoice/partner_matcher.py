"""Vision 抽出した会社名 / 送信者ドメインから freee partner を引き当てる。

partner 未登録のものを本タスクで自動作成はしない（人手レビュー方針）。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from accounting.connectors.freee import FreeeClient


@dataclass
class PartnerMatch:
    partner_id: int | None
    partner_name: str | None
    match_kind: str  # "exact" / "normalized" / "keyword" / "none"
    notes: str = ""


def _normalize(name: str) -> str:
    """NFKCで全角/半角・カナを揃え、株式会社/合同会社/(株)などの法人形態語を除去する。"""
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", name).strip()
    s = s.replace("　", " ")
    # 法人形態のノイズ語を除去
    for noise in [
        "株式会社",
        "(株)",
        "（株）",
        "有限会社",
        "(有)",
        "（有）",
        "合同会社",
        "(同)",
        "（同）",
        "一般社団法人",
        "公益財団法人",
    ]:
        s = s.replace(noise, "")
    s = re.sub(r"\s+", "", s)
    return s.lower()


def match_partner(
    freee: FreeeClient,
    extracted_partner_name: str | None,
    fallback_hint: str | None = None,
) -> PartnerMatch:
    """freee の partners 一覧から引き当てる。

    Args:
        freee: 認証済みクライアント
        extracted_partner_name: Vision が抽出した会社名
        fallback_hint: blacklists.KNOWN_BANK_TRANSFER_VENDORS の partner_name_hint

    Returns: PartnerMatch
    """
    candidates: list[str] = [
        x for x in (extracted_partner_name, fallback_hint) if x
    ]
    if not candidates:
        return PartnerMatch(None, None, "none", "no_name_to_match")

    partners = freee.list_partners()
    # 完全一致を優先
    for cand in candidates:
        for p in partners:
            name = p.get("name", "")
            shortcut = p.get("shortcut1", "") or p.get("shortcut2", "")
            if cand == name or cand == shortcut:
                return PartnerMatch(
                    partner_id=int(p["id"]),
                    partner_name=name,
                    match_kind="exact",
                    notes=f"matched_by={cand}",
                )

    # 正規化マッチ（カナ・全半角・法人形態を吸収）
    for cand in candidates:
        norm_cand = _normalize(cand)
        if not norm_cand:
            continue
        for p in partners:
            norm_name = _normalize(p.get("name", ""))
            if not norm_name:
                continue
            if norm_cand == norm_name:
                return PartnerMatch(
                    partner_id=int(p["id"]),
                    partner_name=p.get("name", ""),
                    match_kind="normalized",
                    notes=f"matched_by={cand}",
                )

    # 部分一致（候補が会社名に含まれる、その逆も）
    for cand in candidates:
        norm_cand = _normalize(cand)
        if len(norm_cand) < 3:
            continue
        for p in partners:
            norm_name = _normalize(p.get("name", ""))
            if not norm_name:
                continue
            if norm_cand in norm_name or norm_name in norm_cand:
                return PartnerMatch(
                    partner_id=int(p["id"]),
                    partner_name=p.get("name", ""),
                    match_kind="keyword",
                    notes=f"matched_by={cand}（部分一致、要確認）",
                )

    return PartnerMatch(None, None, "none", f"no_match_for={candidates}")


def find_partner_by_id(
    freee: FreeeClient, partner_id: int
) -> dict[str, Any] | None:
    """ID で 1 件取得（list を絞れる API がないので keyword 補助）。"""
    partners = freee.list_partners()
    for p in partners:
        if int(p.get("id", 0)) == partner_id:
            return p
    return None
