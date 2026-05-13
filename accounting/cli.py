from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from accounting.config import settings

app = typer.Typer(help="さとやまコーヒー 月次決算自動化ハブ CLI")
journal_rules_app = typer.Typer(help="自動仕訳ルール管理（user_matchers API）")


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


app.add_typer(journal_rules_app, name="journal-rules")


@journal_rules_app.command("analyze")
def journal_rules_analyze(
    months: int = typer.Option(12, "--months", help="過去N月分の取引を分析対象にする"),
    min_occurrence: int = typer.Option(3, "--min-occurrence", help="採用する最低出現回数"),
    consistency: float = typer.Option(
        1.0, "--consistency", help="最頻勘定科目の一貫率閾値 (0.0-1.0)"
    ),
    output: Path = typer.Option(
        Path("rule_candidates.csv"), "--output", help="CSV 出力先"
    ),
    source: str = typer.Option(
        "both",
        "--source",
        help="抽出元: deals (取引明細) / wallet_txns (口座明細) / both（デフォルト）",
    ),
) -> None:
    """過去取引を分析してルール候補を CSV 出力する（読み取り専用、副作用なし）。"""
    from accounting.core.db import init_db
    from accounting.core.report import generate_run_id
    from accounting.tasks import journal_rules

    init_db()
    run_id = generate_run_id("journal-rules-analyze")
    typer.echo(f"run_id: {run_id}")
    typer.echo(
        f"分析: 過去 {months} ヶ月 / 最低出現 {min_occurrence} 回 / "
        f"一貫率 >= {consistency} / source={source}"
    )

    candidates = journal_rules.run_analyze(
        months_back=months,
        min_occurrence=min_occurrence,
        consistency_threshold=consistency,
        source=source,
    )
    journal_rules.candidates_to_csv(candidates, output)
    n_income = sum(1 for c in candidates if c.entry_side_str == "income")
    n_expense = sum(1 for c in candidates if c.entry_side_str == "expense")
    typer.echo(
        f"✓ {len(candidates)} 件の候補（income={n_income} / expense={n_expense}）を "
        f"{output} に出力しました"
    )
    for c in candidates[:5]:
        typer.echo(
            f"  - [{c.occurrence}回 一貫率{c.consistency:.0%}] "
            f"{c.entry_side_str} / {c.partner_name or '(取引先なし)'} / "
            f"{c.keyword!r} → {c.suggested_account_item_name}"
        )


@journal_rules_app.command("apply")
def journal_rules_apply(
    input_file: Path = typer.Option(
        Path("rule_candidates.csv"), "--input", help="analyze で生成した CSV"
    ),
    dry_run: bool = typer.Option(
        settings.dry_run,
        "--dry-run/--no-dry-run",
        help="dry-run（既定）。本番作成は --no-dry-run を明示",
    ),
    batch_size: int = typer.Option(
        10, "--batch-size", help="一度に作成する最大件数（既定: 10）"
    ),
    interactive: bool = typer.Option(
        True, "--interactive/--no-interactive", help="1件ずつ y/n 確認"
    ),
    auto: bool = typer.Option(
        False, "--auto", help="act=1 (auto_standard、freee側で自動確定) に上書き"
    ),
) -> None:
    """CSV から候補を読み、user_matchers API でルールを作成する。"""
    from rich.console import Console
    from rich.table import Table

    from accounting.connectors.freee import FreeeClient
    from accounting.core.db import init_db
    from accounting.core.dry_run import DryRunContext
    from accounting.core.notifier import notify_failure
    from accounting.core.report import generate_run_id
    from accounting.tasks import journal_rules

    init_db()
    run_id = generate_run_id("journal-rules-apply")
    console = Console()

    candidates = journal_rules.csv_to_candidates(input_file)
    if auto:
        for c in candidates:
            c.act = 1
    candidates = candidates[:batch_size]
    if not candidates:
        typer.echo("候補が0件です。", err=True)
        raise typer.Exit(code=1)

    def confirm(c: journal_rules.RuleCandidate, payload: dict) -> bool:
        if not interactive:
            return True
        t = Table(title=f"候補: {c.keyword!r}", show_header=True)
        t.add_column("キー")
        t.add_column("値")
        t.add_row("entry_side", c.entry_side_str)
        t.add_row("partner", c.partner_name or "(なし)")
        t.add_row("account_item", c.suggested_account_item_name)
        t.add_row("tax_name", c.suggested_tax_name or "(なし)")
        t.add_row("act", str(c.act) + (" (auto_standard)" if c.act == 1 else " (manual_standard)"))
        t.add_row("condition", str(c.condition))
        t.add_row("occurrence / consistency", f"{c.occurrence} / {c.consistency:.0%}")
        console.print(t)
        try:
            ans = input("このルールを作成しますか？ [y/N]: ").strip().lower()
        except EOFError:
            ans = "n"
        return ans in ("y", "yes")

    try:
        with FreeeClient() as freee:
            with DryRunContext(dry_run):
                existing = freee.list_user_matchers()
                account_items = freee.get_account_items()
                valid_names = {a.get("name") for a in account_items if a.get("name")}
                result = journal_rules.apply_rule_candidates(
                    candidates,
                    existing_matchers=existing,
                    valid_account_item_names=valid_names,
                    freee=freee,
                    run_id=run_id,
                    confirm=confirm,
                )
    except Exception as e:
        notify_failure(
            "journal_rules",
            run_id,
            e,
            {"input_file": str(input_file), "batch_size": batch_size},
        )
        raise

    typer.echo(
        f"作成: {len(result.created)} / スキップ: {len(result.skipped_duplicates)} / 失敗: {len(result.failed)}"
    )
    if dry_run:
        typer.echo("[dry-run] freee には何も書き込んでいません。--no-dry-run で本番作成。")
    if result.failed:
        raise typer.Exit(code=1)


@journal_rules_app.command("list")
def journal_rules_list() -> None:
    """freee 上の既存自動仕訳ルール一覧を表示する。"""
    from accounting.connectors.freee import FreeeClient

    with FreeeClient() as freee:
        items = freee.list_user_matchers()
    typer.echo(f"{len(items)} 件のルール")
    for m in items:
        typer.echo(
            f"  id={m.get('id')}  act={m.get('act')}  cond={m.get('condition')}  "
            f"side={m.get('entry_side_str')}  desc={m.get('description')!r}  "
            f"→ {m.get('account_item_name', '')}"
        )


@journal_rules_app.command("delete")
def journal_rules_delete(
    matcher_id: int = typer.Argument(..., help="削除するルールの ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="確認プロンプトをスキップ"),
) -> None:
    """ルールを ID 指定で削除する。"""
    from accounting.connectors.freee import FreeeClient

    if not yes:
        try:
            ans = input(
                f"matcher_id={matcher_id} を freee から削除します。よろしいですか？ [y/N]: "
            ).strip().lower()
        except EOFError:
            ans = "n"
        if ans not in ("y", "yes"):
            typer.echo("中断しました。")
            raise typer.Exit(code=1)

    with FreeeClient() as freee:
        freee.delete_user_matcher(matcher_id)
    typer.echo(f"✓ matcher_id={matcher_id} を削除しました")


@journal_rules_app.command("update")
def journal_rules_update(
    account_filter: str = typer.Option(
        None, "--account-filter", help="account_item_name でフィルタ（例: '売上高'）"
    ),
    entry_side_filter: str = typer.Option(
        None, "--entry-side", help="income / expense で絞る"
    ),
    new_act: int = typer.Option(
        None, "--new-act", help="新しい act 値（0=manual_standard, 1=auto_standard）"
    ),
    new_account: str = typer.Option(
        None, "--new-account", help="新しい account_item_name（例: '売上高' → '売掛金'）"
    ),
    new_tax: str = typer.Option(None, "--new-tax", help="新しい tax_name"),
    min_occurrence: int = typer.Option(
        None,
        "--min-occurrence",
        help="CSV の occurrence がこの値以上のルールのみ更新",
    ),
    csv_path: Path = typer.Option(
        Path("rule_candidates.csv"),
        "--csv",
        help="--min-occurrence 使用時に必要",
    ),
    exclude_id: list[int] = typer.Option(
        [],
        "--exclude-id",
        help="このIDを更新対象から除外する（複数指定可: --exclude-id 1 --exclude-id 2）",
    ),
    dry_run: bool = typer.Option(True, "--dry-run/--no-dry-run"),
    interactive: bool = typer.Option(True, "--interactive/--no-interactive"),
) -> None:
    """freee 上の既存自動仕訳ルールを一括更新する。

    例:
      # 売上高ルールを全部 act=1（自動登録）+ account_item="売掛金" に変更
      accounting journal-rules update --account-filter 売上高 --new-act 1 --new-account 売掛金 --no-dry-run --no-interactive

      # 出現10回以上の支出ルールを act=1 に変更
      accounting journal-rules update --entry-side expense --min-occurrence 10 --new-act 1 --no-dry-run --no-interactive
    """
    from accounting.connectors.freee import FreeeClient
    from accounting.core.dry_run import DryRunContext
    from accounting.tasks.journal_rules import bulk_update_rules

    with DryRunContext(dry_run):
        with FreeeClient() as freee:
            result = bulk_update_rules(
                freee,
                account_filter=account_filter,
                new_act=new_act,
                new_account_item_name=new_account,
                new_tax_name=new_tax,
                entry_side_filter=entry_side_filter,
                min_occurrence_filter=min_occurrence,
                csv_path=csv_path if csv_path.exists() else None,
                exclude_ids=exclude_id if exclude_id else None,
                interactive=interactive,
            )
    typer.echo(
        f"更新: {len(result['updated'])} / スキップ: {len(result['skipped'])} / 失敗: {len(result['failed'])}"
    )
    if dry_run:
        typer.echo("[dry-run] freee には何も書き込んでいません。--no-dry-run で本番更新。")


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
