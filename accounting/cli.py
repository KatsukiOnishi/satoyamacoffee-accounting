from __future__ import annotations

import json

import typer

from accounting.config import settings

app = typer.Typer(help="さとやまコーヒー 月次決算自動化ハブ CLI")


@app.command()
def ping(
    dry_run: bool = typer.Option(
        settings.dry_run,
        "--dry-run/--no-dry-run",
        help="dry-run で実行する（freeeへの書き込みをスキップ）",
    ),
) -> None:
    """共通基盤の疎通確認。"""
    from accounting.tasks import ping as ping_task

    result = ping_task.run(dry_run=dry_run)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))


@app.command("list-accounts")
def list_accounts() -> None:
    """freee の勘定科目一覧を表示する（初期 env 設定の補助）。"""
    from accounting.connectors.freee import FreeeClient

    if not settings.freee_api_key or not settings.freee_company_id:
        typer.echo("FREEE_API_KEY / FREEE_COMPANY_ID が未設定です。", err=True)
        raise typer.Exit(code=1)

    with FreeeClient() as client:
        items = client.get_account_items()

    for item in items:
        typer.echo(f"{item.get('id')}\t{item.get('name')}")


if __name__ == "__main__":
    app()
