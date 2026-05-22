"""auto-classify CLI（typer サブアプリ）: run / set-mode / get-mode / list。"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import typer
from rich.console import Console
from rich.table import Table

from accounting.config import settings
from accounting.connectors.anthropic_classifier import (
    ClassifierError,
    TextClassifier,
)
from accounting.connectors.freee import FreeeClient
from accounting.core import auto_keiri
from accounting.core.db import init_db
from accounting.core.dry_run import DryRunContext, is_dry_run
from accounting.core.logger import bind_run, get_logger, unbind_run
from accounting.core.notifier import notify_failure
from accounting.core.report import generate_run_id
from accounting.tasks.auto_classify import (
    classifier as classifier_mod,
    excluder,
    fetcher,
    mode_manager,
    registrar,
)
from accounting.tasks.auto_classify.models import (
    AutoClassifyRunResult,
    ClassificationResult,
    WalletTxnForClassify,
)

auto_classify_app = typer.Typer(help="信頼度付きの自動仕訳（shadow / production）")
console = Console()
log = get_logger("auto_classify")

TASK_NAME = "auto_classify"


@auto_classify_app.command("run")
def run(
    days: int = typer.Option(14, "--days", help="過去N日分の wallet_txn を対象"),
    dry_run: bool = typer.Option(
        settings.dry_run,
        "--dry-run/--no-dry-run",
        help="dry-run（既定）。本番登録は --no-dry-run を明示",
    ),
    limit: int = typer.Option(
        50,
        "--limit",
        help="1 回の実行で Claude に投げる最大件数（API コスト保護、 0 で無制限）",
    ),
) -> None:
    """未紐付 wallet_txn を Claude で分類し、信頼度に応じて freee 登録 or candidate 記録する。"""
    init_db()
    run_id = generate_run_id(TASK_NAME)
    bind_run(TASK_NAME, run_id)
    mode = mode_manager.get_mode()
    high, low = mode_manager.get_thresholds()
    typer.echo(
        f"run_id: {run_id}  mode={mode}  dry_run={dry_run}  days={days}  "
        f"thresholds=({high},{low})"
    )

    end = date.today()
    start = end - timedelta(days=days)
    company_id = int(settings.freee_company_id or 0)
    if not company_id:
        typer.echo("FREEE_COMPANY_ID が未設定です。", err=True)
        raise typer.Exit(code=1)

    results: list[ClassificationResult] = []
    summary = AutoClassifyRunResult(run_id=run_id, mode=mode)

    try:
        with DryRunContext(dry_run):
            with FreeeClient() as freee:
                txns = fetcher.fetch_unreconciled_wallet_txns(
                    freee, start_date=start, end_date=end
                )
                summary.total_fetched = len(txns)

                # ar-reconcile 済の wallet_txn を除外
                already = auto_keiri.get_reconciled_wallet_txn_ids()
                filtered: list[WalletTxnForClassify] = []
                for t in txns:
                    if t.id in already:
                        summary.total_excluded += 1
                        continue
                    reason = excluder.auto_classify_exclusion_reason(
                        t.description, t.amount
                    )
                    if reason:
                        summary.total_excluded += 1
                        log.info(
                            "auto_classify.excluded",
                            wallet_txn_id=t.id,
                            reason=reason,
                        )
                        continue
                    filtered.append(t)
                log.info(
                    "auto_classify.after_exclude",
                    fetched=len(txns),
                    excluded=summary.total_excluded,
                    remaining=len(filtered),
                )
                if limit and len(filtered) > limit:
                    log.warning(
                        "auto_classify.limit_truncated",
                        limit=limit,
                        truncated=len(filtered) - limit,
                    )
                    filtered = filtered[:limit]

                if not filtered:
                    _persist_and_render(results, summary, mode, dry_run)
                    unbind_run()
                    return

                # Claude 投入用に freee マスタを 1 回だけ取得
                masters = fetcher.fetch_freee_masters(freee)
                past_examples = fetcher.fetch_past_examples(freee)

                try:
                    text_classifier = TextClassifier()
                except ClassifierError as e:
                    typer.echo(f"Claude 未設定: {e}", err=True)
                    raise typer.Exit(code=2)

                for txn in filtered:
                    result = _classify_and_register(
                        txn,
                        freee=freee,
                        text_classifier=text_classifier,
                        masters=masters,
                        past_examples=past_examples,
                        mode=mode,
                        company_id=company_id,
                        run_id=run_id,
                        high=high,
                        low=low,
                    )
                    results.append(result)
                    _bump_summary(summary, result)

                _persist_and_render(results, summary, mode, dry_run)
    except typer.Exit:
        raise
    except Exception as e:
        log.exception("auto_classify.run_failed")
        notify_failure(TASK_NAME, run_id, e, {"days": days, "dry_run": dry_run})
        unbind_run()
        raise
    finally:
        unbind_run()


def _classify_and_register(
    txn: WalletTxnForClassify,
    *,
    freee: FreeeClient,
    text_classifier: TextClassifier,
    masters: dict,
    past_examples: list,
    mode: str,
    company_id: int,
    run_id: str,
    high: float,
    low: float,
) -> ClassificationResult:
    result = ClassificationResult(wallet_txn=txn)
    try:
        out = classifier_mod.classify_one(
            txn,
            classifier=text_classifier,
            account_items=masters["account_items"],
            partners=masters["partners"],
            past_examples=past_examples,
        )
        result.output = out
    except Exception as e:
        log.exception("auto_classify.classify_failed", wallet_txn_id=txn.id)
        result.action_taken = "failed"
        result.error_message = f"classify_failed: {type(e).__name__}: {e}"
        return result

    ai_id, tax_id, partner_id = classifier_mod.resolve_masters(out, masters)
    result.resolved_account_item_id = ai_id
    result.resolved_tax_code_id = tax_id
    result.resolved_partner_id = partner_id

    if mode == auto_keiri.MODE_SHADOW:
        result.action_taken = "shadow_logged"
        return result

    # production モード: しきい値で分岐
    confidence = out.confidence
    if confidence >= high and ai_id is not None and tax_id is not None:
        result = registrar.register_deal_for_classification(
            freee, result, company_id=company_id, mode=mode, run_id=run_id
        )
        return result
    if confidence >= low:
        result.action_taken = "review_required"
        return result
    result.action_taken = "skipped"
    return result


def _bump_summary(
    summary: AutoClassifyRunResult, result: ClassificationResult
) -> None:
    a = result.action_taken
    if a == "shadow_logged":
        summary.shadow_logged += 1
    elif a == "registered":
        summary.registered += 1
    elif a == "review_required":
        summary.review_required += 1
    elif a == "skipped":
        summary.skipped += 1
    elif a == "failed":
        summary.failed += 1


def _persist_and_render(
    results: list[ClassificationResult],
    summary: AutoClassifyRunResult,
    mode: str,
    dry_run: bool,
) -> None:
    for r in results:
        out = r.output
        auto_keiri.insert_classify_candidate(
            run_id=summary.run_id,
            run_started_at=datetime.utcnow(),
            mode=mode,
            wallet_txn_id=int(r.wallet_txn.id),
            wallet_txn_date=r.wallet_txn.date,
            wallet_txn_description=r.wallet_txn.description,
            wallet_txn_amount=int(r.wallet_txn.amount),
            wallet_txn_walletable_name=r.wallet_txn.walletable_name,
            classified_account_item_id=r.resolved_account_item_id,
            classified_account_item_name=out.account_item_name if out else None,
            classified_tax_code_id=r.resolved_tax_code_id,
            classified_tax_code_name=out.tax_code_name if out else None,
            classified_partner_id=r.resolved_partner_id,
            classified_partner_name=out.partner_name if out else None,
            classification_confidence=out.confidence if out else None,
            classification_reason=out.reason if out else None,
            classification_alternative=classifier_mod.serialize_alternative(out)
            if out
            else None,
            action_taken=r.action_taken,
            freee_deal_id=r.freee_deal_id,
            error_message=r.error_message,
        )

    t = Table(
        title=f"auto-classify 結果 (mode={mode}, dry_run={dry_run})", show_header=True
    )
    t.add_column("metric")
    t.add_column("count", justify="right")
    t.add_row("total_fetched", str(summary.total_fetched))
    t.add_row("excluded", str(summary.total_excluded))
    t.add_row("shadow_logged", str(summary.shadow_logged))
    t.add_row("registered", str(summary.registered))
    t.add_row("review_required", str(summary.review_required))
    t.add_row("skipped", str(summary.skipped))
    t.add_row("failed", str(summary.failed))
    console.print(t)

    t2 = Table(title="auto-classify 明細（先頭20件）", show_header=True)
    t2.add_column("action")
    t2.add_column("conf", justify="right")
    t2.add_column("date")
    t2.add_column("amount", justify="right")
    t2.add_column("description")
    t2.add_column("→ account_item")
    t2.add_column("tax")
    for r in results[:20]:
        out = r.output
        t2.add_row(
            r.action_taken,
            f"{out.confidence:.2f}" if out else "",
            r.wallet_txn.date.isoformat(),
            f"{r.wallet_txn.amount:,}",
            (r.wallet_txn.description or "")[:24],
            (out.account_item_name if out else "") or "",
            (out.tax_code_name if out else "") or "",
        )
    console.print(t2)
    if is_dry_run():
        typer.echo("[dry-run] freee には何も書き込んでいません。")


# --------------- set-mode / get-mode --------------- #


@auto_classify_app.command("set-mode")
def set_mode(
    mode: str = typer.Option(..., "--mode", help="shadow / production"),
    reason: str = typer.Option(
        "manual", "--reason", help="モード変更理由（system_settings に保存）"
    ),
) -> None:
    """auto-classify モードを切り替える（system_settings 更新）。"""
    init_db()
    old = mode_manager.get_mode()
    try:
        mode_manager.set_mode(mode, reason=reason)
    except ValueError as e:
        typer.echo(f"✗ {e}", err=True)
        raise typer.Exit(code=2)
    typer.echo(f"✓ mode: {old} → {mode}  reason={reason!r}")

    # 切替通知（同じ shadow→shadow なら送らない）
    if old != mode:
        from accounting.core import notifier as core_notifier

        subject = f"[さとやま経理] auto-classify モード切替: {old} → {mode}"
        body = (
            f"auto-classify のモードを {old} → {mode} に切り替えました。\n\n"
            f"理由: {reason}\n"
            f"日時: {datetime.now().isoformat()}\n"
        )
        try:
            core_notifier._send(subject, body)
        except Exception as e:
            typer.echo(f"  （通知メール送信失敗: {e}）", err=True)


@auto_classify_app.command("get-mode")
def get_mode() -> None:
    """現在の auto-classify モードを表示する。"""
    init_db()
    mode = mode_manager.get_mode()
    high, low = mode_manager.get_thresholds()
    typer.echo(f"mode: {mode}")
    typer.echo(f"threshold_high: {high}")
    typer.echo(f"threshold_low : {low}")


@auto_classify_app.command("set-threshold")
def set_threshold(
    high: float = typer.Option(None, "--high", help="新しい高信頼度しきい値"),
    low: float = typer.Option(None, "--low", help="新しい低信頼度しきい値"),
) -> None:
    """信頼度しきい値を変更する。"""
    init_db()
    changes: dict[str, float] = {}
    if high is not None:
        if not 0.0 <= high <= 1.0:
            typer.echo("--high は 0.0-1.0", err=True)
            raise typer.Exit(code=2)
        auto_keiri.set_setting(
            auto_keiri.AUTO_CLASSIFY_THRESHOLD_HIGH_KEY, f"{high}", "manual"
        )
        changes["high"] = high
    if low is not None:
        if not 0.0 <= low <= 1.0:
            typer.echo("--low は 0.0-1.0", err=True)
            raise typer.Exit(code=2)
        auto_keiri.set_setting(
            auto_keiri.AUTO_CLASSIFY_THRESHOLD_LOW_KEY, f"{low}", "manual"
        )
        changes["low"] = low
    if not changes:
        typer.echo("--high か --low のどちらかを指定してください", err=True)
        raise typer.Exit(code=2)
    typer.echo(f"✓ thresholds updated: {changes}")


@auto_classify_app.command("list")
def list_candidates(
    week: str = typer.Option(
        None,
        "--week",
        help="ISO 週指定（例: 2026-W21）。省略時は直近7日",
    ),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """auto_classify_candidates の一覧表示。"""
    init_db()
    if week:
        try:
            start = date.fromisocalendar(*_parse_iso_week(week), 1)
            end = start + timedelta(days=6)
        except ValueError as e:
            typer.echo(f"--week パース失敗: {e}", err=True)
            raise typer.Exit(code=2)
    else:
        end = date.today()
        start = end - timedelta(days=7)
    rows = auto_keiri.list_classify_candidates_in_range(start, end)[:limit]
    if not rows:
        typer.echo(f"({start} 〜 {end} 該当なし)")
        return

    t = Table(title=f"auto-classify {start} 〜 {end} (max {limit})", show_header=True)
    t.add_column("date")
    t.add_column("action")
    t.add_column("conf", justify="right")
    t.add_column("amount", justify="right")
    t.add_column("desc")
    t.add_column("→ account_item")
    for r in rows:
        t.add_row(
            r.wallet_txn_date.isoformat(),
            r.action_taken,
            f"{(r.classification_confidence or 0):.2f}",
            f"{r.wallet_txn_amount:,}",
            (r.wallet_txn_description or "")[:24],
            r.classified_account_item_name or "",
        )
    console.print(t)


def _parse_iso_week(week: str) -> tuple[int, int]:
    """'2026-W21' を (year, week) に分解する。"""
    if "W" not in week:
        raise ValueError("形式は 'YYYY-Www'（例: 2026-W21）")
    year, w = week.split("-W", 1)
    return int(year), int(w)


__all__ = ["auto_classify_app"]
