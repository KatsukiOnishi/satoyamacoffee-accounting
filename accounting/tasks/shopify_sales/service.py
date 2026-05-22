"""shopify-sales サービス層（CLI/Web 両方から呼ばれるオーケストレーション）。

1. Shopify から前月 Orders を取得
2. aggregate() で MonthlySummary に集計
3. build_manual_journal_payload() で freee payload 化
4. dry-run なら preview 表示のみ、本番なら create_manual_journal
5. 冪等性 + RunReport + 通知
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from rich.console import Console
from rich.table import Table

from accounting.connectors.freee import FreeeClient
from accounting.connectors.shopify import ShopifyClient
from accounting.core.db import init_db
from accounting.core.dry_run import is_dry_run
from accounting.core.idempotency import get_execution, is_executed, mark_executed
from accounting.core.logger import bind_run, get_logger, unbind_run
from accounting.core.notifier import notify_failure
from accounting.core.report import RunReport, generate_run_id
from accounting.tasks.shopify_sales import env as _env
from accounting.tasks.shopify_sales.aggregator import aggregate
from accounting.tasks.shopify_sales.freee_writer import (
    AccountIds,
    build_external_id,
    build_manual_journal_payload,
    resolve_account_ids,
)
from accounting.tasks.shopify_sales.models import MonthlySummary

console = Console()
log = get_logger("shopify-sales")

TASK_NAME = "shopify-sales"


@dataclass
class PreviewResult:
    summary: MonthlySummary
    payload: dict[str, Any] | None  # 0件月は None
    external_id: str
    already_executed: dict[str, Any] | None = None  # 既登録時の execution row
    warnings: list[str] = field(default_factory=list)


def parse_month(month: str) -> tuple[int, int]:
    if len(month) != 7 or month[4] != "-":
        raise ValueError(f"month は YYYY-MM 形式で指定: {month!r}")
    try:
        y, m = int(month[:4]), int(month[5:])
    except ValueError as e:
        raise ValueError(f"month は YYYY-MM 形式で指定: {month!r}") from e
    if not (1 <= m <= 12):
        raise ValueError(f"month の月部分が不正: {month!r}")
    return y, m


def _fetch_orders(year: int, month: int) -> list[dict[str, Any]]:
    with ShopifyClient() as sc:
        return sc.list_orders_for_jst_month(year=year, month=month)


def build_preview(
    *,
    month: str,
    orders_loader: Callable[[int, int], list[dict[str, Any]]] = _fetch_orders,
    account_ids: Optional[AccountIds] = None,
    freee: Optional[FreeeClient] = None,
) -> PreviewResult:
    """Shopify Orders 取得 → 集計 → payload 構築までを実施し、副作用なしで返す。

    `orders_loader` を差し替えればテストで fixture を流せる。
    """
    init_db()
    year, mon = parse_month(month)
    external_id = build_external_id(year, mon)

    already = get_execution(TASK_NAME, external_id)
    already_success = already and already.get("status") == "success"

    orders = orders_loader(year, mon)
    summary = aggregate(
        year=year,
        month=mon,
        orders=orders,
        komoju_fee_rate=_env.komoju_fee_rate(),
    )

    if summary.order_count == 0:
        return PreviewResult(
            summary=summary,
            payload=None,
            external_id=external_id,
            already_executed=already if already_success else None,
            warnings=summary.warnings,
        )

    if account_ids is None:
        account_ids = resolve_account_ids(freee=freee)

    payload = build_manual_journal_payload(
        summary=summary,
        company_id=_env.company_id(),
        account_ids=account_ids,
    )

    return PreviewResult(
        summary=summary,
        payload=payload,
        external_id=external_id,
        already_executed=already if already_success else None,
        warnings=summary.warnings,
    )


def render_preview(result: PreviewResult) -> None:
    """rich で preview を表示する。"""
    s = result.summary
    title = (
        f"Shopify売上 {s.year}-{s.month:02d} 集計プレビュー "
        f"({s.period_start_jst} 〜 {s.period_end_jst} JST)"
    )

    head = Table(title=title, show_header=False)
    head.add_column("項目")
    head.add_column("値", justify="right")
    head.add_row("対象 Order", f"{s.order_count} 件")
    head.add_row("除外 Order", f"{s.excluded_count} 件")
    head.add_row("売上総額 (税込)", f"¥{s.total_gross:,}")
    head.add_row("決済手数料合計", f"¥{s.total_fee:,}")
    head.add_row("純入金額合計", f"¥{s.total_net:,}")
    console.print(head)

    if s.by_partner:
        tbl = Table(title="partner 別内訳", show_header=True)
        tbl.add_column("Partner")
        tbl.add_column("件数", justify="right")
        tbl.add_column("Gross", justify="right")
        tbl.add_column("Fee", justify="right")
        tbl.add_column("Net", justify="right")
        tbl.add_column("Gateways")
        for ps in s.by_partner.values():
            tbl.add_row(
                ps.partner_name,
                str(ps.order_count),
                f"¥{ps.gross:,}",
                f"¥{ps.fee:,}",
                f"¥{ps.net:,}",
                ", ".join(sorted(ps.gateways)),
            )
        console.print(tbl)

    if result.payload:
        j = Table(
            title=(
                f"freee 振替伝票プレビュー (issue_date={result.payload['issue_date']}, "
                f"ref={result.external_id})"
            ),
            show_header=True,
        )
        j.add_column("方向")
        j.add_column("勘定科目ID", justify="right")
        j.add_column("税区分", justify="right")
        j.add_column("取引先ID", justify="right")
        j.add_column("金額", justify="right")
        j.add_column("摘要")
        for d in result.payload["details"]:
            j.add_row(
                d["entry_side"],
                str(d["account_item_id"]),
                str(d.get("tax_code", "")),
                str(d.get("partner_id", "")),
                f"¥{d['amount']:,}",
                d["description"],
            )
        console.print(j)
    else:
        console.print("[yellow]対象 Order が0件のため、登録予定の仕訳はありません。[/yellow]")

    if result.warnings:
        console.print("[bold yellow]警告:[/bold yellow]")
        for w in result.warnings:
            console.print(f"  - {w}")

    if result.already_executed:
        ae = result.already_executed
        console.print(
            f"[bold cyan]既登録: external_id={ae.get('external_id')} "
            f"manual_journal_id={ae.get('freee_journal_id') or '-'} "
            f"(at {ae.get('updated_at')})[/bold cyan]"
        )


def run(
    *,
    month: str,
    run_id: Optional[str] = None,
    confirm: bool = True,
    orders_loader: Callable[[int, int], list[dict[str, Any]]] = _fetch_orders,
) -> RunReport:
    """preview → 確認 → freee 登録までを実行する。

    Args:
        month: "YYYY-MM"
        run_id: 省略時は自動生成
        confirm: True なら本番登録前に y/N プロンプト
        orders_loader: テスト時に差し替え可能
    """
    run_id = run_id or generate_run_id(TASK_NAME)
    bind_run(TASK_NAME, run_id)
    report = RunReport(task=TASK_NAME, run_id=run_id)
    log.info("shopify_sales.start", month=month, dry_run=is_dry_run())

    try:
        with FreeeClient() as freee:
            result = build_preview(month=month, orders_loader=orders_loader, freee=freee)
            render_preview(result)

            for w in result.warnings:
                report.add_warning(result.external_id, w)

            if result.already_executed:
                log.warning(
                    "shopify_sales.skip_duplicate",
                    external_id=result.external_id,
                )
                report.add_warning(result.external_id, "already executed")
                report.finalize()
                return report

            if result.payload is None:
                log.warning("shopify_sales.no_orders", month=month)
                report.add_warning(result.external_id, "no_orders_for_month")
                report.finalize()
                return report

            if is_dry_run():
                console.print(
                    "[yellow]dry-run モードのため freee には登録しません。"
                    "本番登録には --no-dry-run を指定してください。[/yellow]"
                )
                report.add_success(result.external_id, "dry-run preview ok")
                report.finalize()
                return report

            if confirm:
                try:
                    answer = input(
                        f"\nfreee に振替伝票を登録します。よろしいですか？ [y/N]: "
                    ).strip().lower()
                except EOFError:
                    answer = "n"
                if answer not in ("y", "yes"):
                    log.info("shopify_sales.aborted_by_user")
                    report.add_warning(result.external_id, "aborted by user")
                    report.finalize()
                    return report

            if is_executed(TASK_NAME, result.external_id):
                # confirm 中の他プロセス完了などの保険
                log.warning(
                    "shopify_sales.skip_duplicate_after_confirm",
                    external_id=result.external_id,
                )
                report.add_warning(result.external_id, "already executed (race)")
                report.finalize()
                return report

            api_result = freee.create_manual_journal(
                result.payload,
                external_id=result.external_id,
                task=TASK_NAME,
            )
            manual_journal_id = api_result.get("manual_journal_id")
            mark_executed(
                TASK_NAME,
                result.external_id,
                run_id,
                str(manual_journal_id or ""),
                "success",
            )
            report.add_success(
                result.external_id,
                f"registered: manual_journal_id={manual_journal_id}",
            )
    except Exception as e:
        log.exception("shopify_sales.failed", error=str(e))
        report.add_failure(month, str(e))
        if not is_dry_run():
            notify_failure(TASK_NAME, run_id, e, {"month": month})
    finally:
        unbind_run()

    report.finalize()
    return report


def status(months: int = 6) -> list[dict[str, Any]]:
    """過去 N 月の登録状況を一覧で返す（status コマンド用）。"""
    from datetime import date

    init_db()
    today = date.today()
    out: list[dict[str, Any]] = []
    y, m = today.year, today.month
    for _ in range(months):
        m -= 1
        if m == 0:
            m = 12
            y -= 1
        ext_id = build_external_id(y, m)
        rec = get_execution(TASK_NAME, ext_id)
        out.append(
            {
                "month": f"{y:04d}-{m:02d}",
                "external_id": ext_id,
                "status": (rec or {}).get("status", "(未実行)"),
                "manual_journal_id": (rec or {}).get("freee_journal_id"),
                "executed_at": (rec or {}).get("updated_at"),
            }
        )
    return out
