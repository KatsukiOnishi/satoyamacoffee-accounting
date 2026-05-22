"""email-digest タスク: 週次ダイジェストメール送信。

仕様書 §5-5 参照。
"""
from accounting.tasks.email_digest.cli import email_digest_app

__all__ = ["email_digest_app"]
