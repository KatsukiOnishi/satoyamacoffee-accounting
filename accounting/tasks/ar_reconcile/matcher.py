"""売掛金消込のマッチングロジック。

純粋関数で、freee API は触らない（fetcher.py で取得した結果を入力として受け取る）。

仕様書 §5-1 マッチング方針:
  1. 候補A: partner_name 正規化一致 AND total_amount 完全一致
  2. 候補B: 候補Aがなければ total_amount 完全一致のみ
  3. 1件 → reconciled / 複数 → multiple_matches / 0 → unmatched
"""
from __future__ import annotations

import re

from accounting.tasks.ar_reconcile.models import (
    ARMatchCandidate,
    UnsettledInvoice,
    WalletTxnIncome,
)


# 法人略称・記号を取り除いて比較しやすくする
_NORMALIZE_RE = re.compile(
    r"(株式会社|有限会社|合同会社|合資会社|合名会社|\(株\)|（株）|"
    r"\(有\)|（有）|\(同\)|（同）|"
    r"カ\)|カ）|\(カ|（カ|"
    r"\s+|・|\.|，|,|‐|‑|−|\-|\(|\)|（|）|株式會社)"
)


def normalize_partner_name(name: str | None) -> str:
    """取引先名を比較しやすい形に正規化する。

    - 法人略称・カッコ・記号・空白を除去
    - 全角カタカナ⇄半角カタカナの正規化（NFKC）
    """
    if not name:
        return ""
    import unicodedata

    s = unicodedata.normalize("NFKC", name)
    s = _NORMALIZE_RE.sub("", s)
    return s.strip().lower()


def find_invoice_candidates(
    txn: WalletTxnIncome,
    invoices: list[UnsettledInvoice],
) -> list[UnsettledInvoice]:
    """txn に対する候補請求書を返す（仕様書 §5-1 のマッチング規則）。

    - 同金額 AND 同 partner（正規化一致）→ 第一候補
    - 第一候補がなければ「同金額」のみ → 第二候補
    """
    same_amount = [
        inv for inv in invoices if inv.total_amount == int(txn.amount)
    ]
    if not same_amount:
        return []

    txn_norm = normalize_partner_name(txn.description)
    # wallet_txn の description には「振込 株式会社○○」のように接頭辞が混ざる。
    # 「振込」「ATM」「カ）」等を一旦剥がしてから partner 比較を試みる。
    partner_matches = [
        inv
        for inv in same_amount
        if inv.partner_name
        and normalize_partner_name(inv.partner_name)
        and normalize_partner_name(inv.partner_name) in txn_norm
    ]
    if partner_matches:
        return partner_matches
    return same_amount


def match_txn(
    txn: WalletTxnIncome,
    invoices: list[UnsettledInvoice],
) -> ARMatchCandidate:
    """1件の wallet_txn についてマッチ結果を返す。"""
    candidates = find_invoice_candidates(txn, invoices)
    if len(candidates) == 1:
        return ARMatchCandidate(
            wallet_txn=txn,
            candidates=candidates,
            status="matched",  # 消込 API 呼び出し前の中間状態
        )
    if len(candidates) >= 2:
        return ARMatchCandidate(
            wallet_txn=txn,
            candidates=candidates,
            status="multiple_matches",
        )
    return ARMatchCandidate(
        wallet_txn=txn,
        candidates=[],
        status="unmatched",
    )
