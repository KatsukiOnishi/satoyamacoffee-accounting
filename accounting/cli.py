from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

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


@app.command("dept-store-invoice")
def dept_store_invoice(
    vendor: str = typer.Option(..., "--vendor", help="取引先 slug（例: seibu）"),
    files: Optional[list[Path]] = typer.Option(
        None, "--files", help="画像ファイル（複数指定可）"
    ),
    dir: Optional[Path] = typer.Option(
        None, "--dir", help="画像ディレクトリ（中の .jpg/.png/.heic を全て処理）"
    ),
    dry_run: bool = typer.Option(
        settings.dry_run,
        "--dry-run/--no-dry-run",
        help="dry-run モード（デフォルト .env の DRY_RUN）",
    ),
) -> None:
    """百貨店明細を写真から取込み、freee に売上仕訳として登録する。"""
    from accounting.core.dry_run import DryRunContext
    from accounting.tasks import dept_store_invoice as task

    image_paths: list[Path] = list(files or [])
    if dir is not None:
        for ext in (".jpg", ".jpeg", ".png", ".heic", ".heif"):
            image_paths.extend(sorted(dir.glob(f"*{ext}")))
            image_paths.extend(sorted(dir.glob(f"*{ext.upper()}")))
    # 重複除去・順序保持
    seen: set[Path] = set()
    unique_paths: list[Path] = []
    for p in image_paths:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            unique_paths.append(p)
    image_paths = unique_paths

    if not image_paths:
        typer.echo("画像ファイルを --files または --dir で指定してください。", err=True)
        raise typer.Exit(code=2)

    with DryRunContext(dry_run):
        report = task.run(image_paths, vendor)

    if report.failure_count > 0:
        raise typer.Exit(code=1)


@app.command("serve")
def serve(
    host: str = typer.Option(
        settings.web_host,
        "--host",
        help="バインドアドレス（LAN内アクセス: 0.0.0.0、ローカル限定: 127.0.0.1）",
    ),
    port: int = typer.Option(settings.web_port, "--port"),
    open_browser: bool = typer.Option(True, "--open-browser/--no-open-browser"),
) -> None:
    """ローカル Web UI サーバを起動する（実行時のみ、Ctrl+C で停止）。"""
    from accounting.web.server import start_server

    start_server(host=host, port=port, open_browser=open_browser)


if __name__ == "__main__":
    app()
