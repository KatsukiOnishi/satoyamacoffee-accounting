"""ar-reconcile CLI（typer サブアプリ）。"""
from __future__ import annotations

from datetime import date, timedelta

import typer
from rich.console import Console
from rich.table import Table

from accounting.config import settings
from accounting.connectors.freee import FreeeClient
from accounting.core import auto_keiri
from accounting.core.db import init_db
from accounting.core.dry_run import DryRunContext, is_dry_run
from accounting.core.logger import bind_run, get_logger, unbind_run
from accounting.core.notifier import notify_failure
from accounting.core.report import generate_run_id
from accounting.tasks.ar_reconcile import excluder, fetcher, matcher, reconciler
from accounting.tasks.ar_reconcile.models import (
    ARMatchCandidate,
    ReconcileRunResult,
    WalletTxnIncome,
)

ar_reconcile_app = typer.Typer(
    help="売掛金消込（未消込入金を freee 未決済請求書に引き当て）"
)
console = Console()
log = get_logger("ar_reconcile")

TASK_NAME = "ar_reconcile"


@ar_reconcile_app.command("run")
def run(
    days: int = typer.Option(14, "--days", help="過去N日分の wallet_txn を対象"),
    dry_run: bool = typer.Option(
        settings.dry_run,
        "--dry-run/--no-dry-run",
        help="dry-run（既定）。本番消込は --no-dry-run を明示",
    ),
) -> None:
    """未消込入金を freee 未決済請求書に引き当てて消込する。"""
    init_db()
    run_id = generate_run_id(TASK_NAME)
    bind_run(TASK_NAME, run_id)
    typer.echo(f"run_id: {run_id}  dry_run={dry_run}  days={days}")

    end = date.today()
    start = end - timedelta(days=days)

    try:
        with DryRunContext(dry_run):
            with FreeeClient() as freee:
                incoming = fetcher.fetch_unreconciled_income_wallet_txns(
                    freee, start_date=start, end_date=end
                )
                # 売上未決済は過去 90 日まで遡って候補を集める（仕様書 §5-1）
                inv_start = end - timedelta(days=90)
                invoices = fetcher.fetch_unsettled_income_invoices(
                    freee, start_date=inv_start, end_date=end
                )

                results: list[ARMatchCandidate] = []
                for txn in incoming:
                    reason = excluder.ar_reconcile_exclusion_reason(txn.description)
                    if reason is not None:
                        c = ARMatchCandidate(
                            wallet_txn=txn,
                            candidates=[],
                            status="excluded",
                            error_message=f"excluded:{reason}",
                        )
                        results.append(c)
                        continue
                    candidate = matcher.match_txn(txn, invoices)
                    if candidate.status == "matched":
                        candidate = reconciler.reconcile_match(
                            freee, candidate, run_id=run_id
                        )
                    results.append(candidate)

                _persist_results(results, run_id=run_id)
    except Exception as e:
        log.exception("ar_reconcile.run_failed")
        notify_failure(TASK_NAME, run_id, e, {"days": days, "dry_run": dry_run})
        unbind_run()
        raise

    summary = _summarize(results, run_id)
    _render_summary(results, summary, dry_run)
    unbind_run()
    if summary.failed > 0:
        raise typer.Exit(code=1)


def _persist_results(
    results: list[ARMatchCandidate], *, run_id: str
) -> None:
    for c in results:
        if c.status == "excluded":
            # excluded は記録対象外（ノイズ削減）
            continue
        auto_keiri.insert_ar_candidate(**reconciler.serialize_for_db(c, run_id=run_id))


def _summarize(
    results: list[ARMatchCandidate], run_id: str
) -> ReconcileRunResult:
    r = ReconcileRunResult(run_id=run_id, total=len(results))
    for c in results:
        if c.status == "reconciled":
            r.reconciled += 1
        elif c.status == "unmatched":
            r.unmatched += 1
        elif c.status == "multiple_matches":
            r.multiple_matches += 1
        elif c.status == "failed":
            r.failed += 1
        elif c.status == "excluded":
            r.excluded += 1
    return r


def _render_summary(
    results: list[ARMatchCandidate],
    summary: ReconcileRunResult,
    dry_run: bool,
) -> None:
    t = Table(
        title=f"ar-reconcile 結果 (dry_run={dry_run})",
        show_header=True,
    )
    t.add_column("status")
    t.add_column("count", justify="right")
    for status in (
        "reconciled",
        "unmatched",
        "multiple_matches",
        "failed",
        "excluded",
    ):
        t.add_row(status, str(getattr(summary, status)))
    console.print(t)

    interesting = [
        c
        for c in results
        if c.status in ("reconciled", "unmatched", "multiple_matches", "failed")
    ]
    if not interesting:
        return
    t2 = Table(
        title="ar-reconcile 明細（先頭20件）",
        show_header=True,
    )
    t2.add_column("status")
    t2.add_column("date")
    t2.add_column("description")
    t2.add_column("amount", justify="right")
    t2.add_column("matched partner")
    t2.add_column("deal_id")
    for c in interesting[:20]:
        txn: WalletTxnIncome = c.wallet_txn
        inv = c.candidates[0] if c.candidates else None
        t2.add_row(
            c.status,
            txn.date.isoformat(),
            (txn.description or "")[:30],
            f"{txn.amount:,}",
            (inv.partner_name or "") if inv else "",
            str(inv.deal_id) if inv else "",
        )
    console.print(t2)
    if is_dry_run():
        typer.echo("[dry-run] freee には何も書き込んでいません。")


__all__ = ["ar_reconcile_app"]
