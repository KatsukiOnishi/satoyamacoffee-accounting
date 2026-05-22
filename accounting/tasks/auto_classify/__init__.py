"""auto-classify タスク: 信頼度付きの自動仕訳。

仕様書 §5-2 〜 5-4 参照。
シャドーモード（初期2週間）→精度確認→本番モード（信頼度>0.85なら自動登録）。
"""
from accounting.tasks.auto_classify.cli import auto_classify_app

__all__ = ["auto_classify_app"]
