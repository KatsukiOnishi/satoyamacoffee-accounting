"""shopify-sales サブコマンド (run / preview / status)。

`accounting/cli.py` から `shopify_sales_app` をマウントする。
"""
from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from accounting.config import settings
from accounting.core.dry_run import DryRunContext
from accounting.tasks.shopify_sales import service

shopify_sales_app = typer.Typer(
    help="Shopify 月次売上 → freee 振替伝票"
)
console = Console()


@shopify_sales_app.command("run")
def run_cmd(
    month: str = typer.Option(..., "--month", help="対象月 YYYY-MM (例: 2026-04)"),
    dry_run: bool = typer.Option(
        settings.dry_run,
        "--dry-run/--no-dry-run",
        help="dry-run モード（既定）。本番登録は --no-dry-run を明示",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="本番登録時の確認プロンプトをスキップ"
    ),
) -> None:
    """Shopify Orders を集計して freee 振替伝票に登録する。"""
    with DryRunContext(dry_run):
        report = service.run(month=month, confirm=not yes)
    if report.failure_count > 0:
        raise typer.Exit(code=1)


@shopify_sales_app.command("preview")
def preview_cmd(
    month: str = typer.Option(..., "--month", help="対象月 YYYY-MM"),
) -> None:
    """dry-run と同じ集計を表示するだけ（freee には触れない）。"""
    with DryRunContext(True):
        result = service.build_preview(month=month)
        service.render_preview(result)


@shopify_sales_app.command("status")
def status_cmd(
    months: int = typer.Option(6, "--months", help="表示する過去 N 月"),
) -> None:
    """過去 N 月の登録状況一覧。"""
    rows = service.status(months=months)
    t = Table(title=f"shopify-sales 登録状況 (過去 {months} 月)", show_header=True)
    t.add_column("月")
    t.add_column("external_id")
    t.add_column("status")
    t.add_column("manual_journal_id")
    t.add_column("executed_at")
    for r in rows:
        t.add_row(
            r["month"],
            r["external_id"],
            str(r["status"]),
            str(r.get("manual_journal_id") or "-"),
            str(r.get("executed_at") or "-"),
        )
    console.print(t)
