"""vendor-invoice: ベンダー請求書メール → freee 取引登録 + 未払金消し込み。

CLIエントリは `accounting.cli` から `vendor_invoice_app` をインポートして配線する。
"""
from accounting.tasks.vendor_invoice.cli import vendor_invoice_app

__all__ = ["vendor_invoice_app"]
