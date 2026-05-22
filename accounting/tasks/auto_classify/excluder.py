"""auto-classify 用の除外フィルタ（薄いラッパ）。

ar_reconcile.excluder の純粋関数を再利用する。仕様書 §5-3 の除外条件:

  | 条件 | 理由 |
  |---|---|
  | ar-reconcile で `reconciled` 済 | 重複処理防止（呼び出し側で除外）
  | 振込 + 個人名カナ | 給与仕訳タスク
  | そごう / 西武 | 百貨店明細取込タスク
  | 日本公庫 / 公庫 | 借入返済按分（手動）
  | 振込手数料 100-300円 | journal-rules で対応
  | 銀行利息 | 取引額小、ルール化済み
"""
from __future__ import annotations

from accounting.tasks.ar_reconcile.excluder import auto_classify_exclusion_reason

__all__ = ["auto_classify_exclusion_reason"]
