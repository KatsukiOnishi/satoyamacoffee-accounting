"""ar-reconcile タスク: 売掛金消込（法人入金 → freee 未決済請求書）。

仕様書 §5-1 参照。
"""
from accounting.tasks.ar_reconcile.cli import ar_reconcile_app

__all__ = ["ar_reconcile_app"]
