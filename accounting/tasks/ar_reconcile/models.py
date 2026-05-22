"""ar-reconcile タスクのデータモデル（Pydantic）。"""
from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel


class WalletTxnIncome(BaseModel):
    """freee の wallet_txn（type=income 想定）の必要フィールドだけ抽出した形。"""

    id: int
    date: date
    description: str
    amount: int                      # 正値（入金）
    walletable_type: str | None = None
    walletable_id: int | None = None
    walletable_name: str | None = None
    entry_side: str | None = None


class UnsettledInvoice(BaseModel):
    """freee の未決済請求書（deal type=income, status=unsettled）の要約。"""

    deal_id: int
    partner_id: int | None = None
    partner_name: str | None = None
    total_amount: int
    issue_date: date | None = None
    due_date: date | None = None


class ARMatchCandidate(BaseModel):
    """突き合わせ結果。

    status の取り得る値:
      - reconciled        : 1件マッチして消込成功
      - unmatched         : マッチする未決済請求書なし
      - multiple_matches  : 複数マッチして自動消込不可
      - failed            : freee API 呼び出し失敗等
      - excluded          : 振込元が個人名カナ・百貨店等で除外
    """

    wallet_txn: WalletTxnIncome
    candidates: list[UnsettledInvoice] = []
    status: str
    error_message: str | None = None
    freee_response: dict[str, Any] | None = None


class ReconcileRunResult(BaseModel):
    run_id: str
    total: int = 0
    reconciled: int = 0
    unmatched: int = 0
    multiple_matches: int = 0
    failed: int = 0
    excluded: int = 0
