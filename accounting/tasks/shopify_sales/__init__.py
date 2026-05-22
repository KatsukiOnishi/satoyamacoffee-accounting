"""shopify-sales: Shopify EC 月次売上 → freee 振替伝票。

CLI エントリは `accounting.cli` から `shopify_sales_app` をマウントする。
"""
from accounting.tasks.shopify_sales.cli import shopify_sales_app

__all__ = ["shopify_sales_app"]
