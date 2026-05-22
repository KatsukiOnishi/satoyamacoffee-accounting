"""ar-reconcile / auto-classify 共通の除外フィルタ。

仕様書 §5-1（ar-reconcile 除外）と §5-3（auto-classify 除外フィルタ）の純粋関数群。
"""
from __future__ import annotations

import re

# 「振込 + 個人名カナ」のパターン。サンプル: 「振込 カネコ マリ」「振込 ミナト チホ」
# 末尾に「カ）」「（カ」「（株」等の法人略称を含まない振込文字列を個人名と判定する。
# `^\s*振込` + 1〜N 個のカタカナトークン（空白区切り）+ 末尾余白
_KATAKANA_NAME_RE = re.compile(
    r"^\s*振込\s+[ァ-ヶー]+(?:\s+[ァ-ヶー]+)*\s*$"
)

# 「振込 カ）ソゴウ．セイブ」「振込 カ）セイブ」「そごう」「西武」を含むパターン
_SEIBU_KEYWORDS = ("そごう", "西武", "ソゴウ", "セイブ")

# 「振込手数料」キーワード + 100-300円
_BANK_FEE_RE = re.compile(r"振込手数料")

# 「日本公庫」「公庫」を含む
_KOUKO_KEYWORDS = ("日本公庫", "公庫")

# 銀行利息系
_INTEREST_KEYWORDS = ("決算お利息", "受取利息", "お利息")


def is_personal_kana_transfer(description: str) -> bool:
    """振込元が個人名カナのみ（法人略称なし）かどうか。

    True なら給与・個人入金として ar-reconcile / auto-classify 両方で除外する。
    """
    if not description:
        return False
    if any(token in description for token in ("カ）", "(カ", "（カ", "(株", "（株", "（有", "(有")):
        return False
    return bool(_KATAKANA_NAME_RE.match(description))


def is_dept_store_transfer(description: str) -> bool:
    """そごう・西武 関連の振込かどうか。"""
    if not description:
        return False
    return any(kw in description for kw in _SEIBU_KEYWORDS)


def is_interest(description: str) -> bool:
    """銀行利息系の摘要かどうか。"""
    if not description:
        return False
    return any(kw in description for kw in _INTEREST_KEYWORDS)


def is_jfc_loan(description: str) -> bool:
    """日本公庫 関連の摘要かどうか（auto-classify では除外）。"""
    if not description:
        return False
    return any(kw in description for kw in _KOUKO_KEYWORDS)


def is_bank_fee(description: str, amount: int) -> bool:
    """振込手数料っぽい摘要（金額 100-300 円）かどうか。"""
    if not description:
        return False
    if not _BANK_FEE_RE.search(description):
        return False
    a = abs(int(amount or 0))
    return 100 <= a <= 300


def ar_reconcile_exclusion_reason(description: str) -> str | None:
    """ar-reconcile の入金 wallet_txn を見て、除外理由があれば返す。

    Returns:
        除外理由の slug（'personal_kana' / 'dept_store' / 'interest'）。
        除外不要なら None。
    """
    if is_personal_kana_transfer(description):
        return "personal_kana"
    if is_dept_store_transfer(description):
        return "dept_store"
    if is_interest(description):
        return "interest"
    return None


def auto_classify_exclusion_reason(
    description: str, amount: int
) -> str | None:
    """auto-classify の wallet_txn を見て、除外理由があれば返す。

    既に ar-reconcile で `reconciled` 済の wallet_txn_id は呼び出し側で除外する
    （description だけでは判定不能なので、本関数の責務外）。
    """
    if is_personal_kana_transfer(description):
        return "personal_kana"
    if is_dept_store_transfer(description):
        return "dept_store"
    if is_jfc_loan(description):
        return "jfc_loan"
    if is_bank_fee(description, amount):
        return "bank_fee"
    if is_interest(description):
        return "interest"
    return None
