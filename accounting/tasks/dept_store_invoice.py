"""百貨店明細取込タスク。

写真 → Claude Vision で構造化抽出 → freee に売上仕訳として登録する。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field
from rich.console import Console
from rich.table import Table

from accounting.config import settings
from accounting.connectors.anthropic_vision import VisionExtractor
from accounting.connectors.freee import FreeeClient
from accounting.core.db import init_db
from accounting.core.dry_run import is_dry_run
from accounting.core.idempotency import is_executed, mark_executed
from accounting.core.logger import bind_run, get_logger, unbind_run
from accounting.core.notifier import notify_failure
from accounting.core.report import RunReport, generate_run_id

console = Console()


class DeptStoreStatement(BaseModel):
    """買掛金支払明細書から抽出される構造化データ。"""

    vendor_name: str = Field(description="取引先名（例: 株式会社そごう・西武）")
    vendor_registration_number: Optional[str] = Field(
        default=None, description="適格請求書発行事業者の登録番号（T+13桁）"
    )
    period_start: date = Field(description="対象期間開始日")
    period_end: date = Field(description="対象期間終了日（締日）")
    issued_date: date = Field(description="発行日")
    payment_date: date = Field(description="お支払日（振込日）")
    gross_sales: int = Field(description="売価（円、整数）")
    purchase_total: Optional[int] = Field(
        default=None, description="仕入額合計A（円、整数）。明細により無い場合あり"
    )
    transfer_amount: int = Field(description="振込額C（円、整数、実際に振り込まれる金額）")


SYSTEM_PROMPT = """あなたは日本の百貨店から事業者宛に送られる「買掛金支払明細書」を読み取る経理アシスタントです。
複数ページの画像が与えられます。サマリーページ（通常は最終ページ）から以下のフィールドを抽出してください:

- 取引先名（明細書の発行元、例: 株式会社そごう・西武）
- 取引先の登録番号（T+13桁の適格請求書発行事業者番号）
- 対象期間の開始日と終了日（例: 2026/03/01〜2026/03/31締分 → 開始 2026-03-01、終了 2026-03-31）
- 発行日
- お支払日（振込日）
- 売価（グロス売上総額、整数円）
- 仕入額合計A（明細により無い場合は null）
- 振込額C（実際に振り込まれる金額、整数円）

日付はすべて ISO 8601 (YYYY-MM-DD) 形式。金額は整数（カンマや円記号を除く）。
不明なフィールドはJSON null を入れること（推測で値を入れない）。
"""


def _build_journal_payload(stmt: DeptStoreStatement, vendor_slug: str) -> dict:
    """freee 仕訳 payload を構築する。妥当性検査も兼ねる。"""
    fee = stmt.gross_sales - stmt.transfer_amount
    if fee < 0:
        raise ValueError(
            f"差額が負（売価={stmt.gross_sales} < 振込額={stmt.transfer_amount}）。"
            "データ抽出ミスの可能性があります"
        )
    partner_id = settings.vendor_partner_id(vendor_slug)
    return {
        "company_id": int(settings.freee_company_id) if settings.freee_company_id else 0,
        "issue_date": stmt.period_end.isoformat(),
        "type": "general",
        "description": (
            f"{stmt.vendor_name} {stmt.period_start}〜{stmt.period_end} 売上計上"
        ),
        "details": [
            {
                "account_item_id": int(settings.freee_account_item_receivable)
                if settings.freee_account_item_receivable
                else 0,
                "tax_code": settings.freee_tax_code_fee,  # 売掛金は税区分なし扱い
                "amount": stmt.transfer_amount,
                "entry_side": "debit",
                "partner_id": partner_id,
                "description": "売掛金（振込額）",
            },
            {
                "account_item_id": int(settings.freee_account_item_commission)
                if settings.freee_account_item_commission
                else 0,
                "tax_code": settings.freee_tax_code_fee,
                "amount": fee,
                "entry_side": "debit",
                "partner_id": partner_id,
                "description": "支払手数料（売価−振込額の差額）",
            },
            {
                "account_item_id": int(settings.freee_account_item_sales)
                if settings.freee_account_item_sales
                else 0,
                "tax_code": settings.freee_tax_code_sales,
                "amount": stmt.gross_sales,
                "entry_side": "credit",
                "partner_id": partner_id,
                "description": "売上高（売価）",
            },
        ],
    }


def _build_external_id(vendor_slug: str, period_end: date) -> str:
    return f"dept-store-{vendor_slug}-{period_end:%Y%m%d}"


# ---- CLI / Web 両方から呼ばれる純粋関数（副作用を抑えた3段階） ----


def extract_statement(image_paths: list[Path]) -> DeptStoreStatement:
    """Vision API で画像群から構造化データを抽出するだけ。副作用なし。"""
    extractor = VisionExtractor()
    return extractor.extract(image_paths, DeptStoreStatement, SYSTEM_PROMPT)


def build_journal_with_validation(
    stmt: DeptStoreStatement, vendor_slug: str
) -> tuple[dict, str, list[str]]:
    """payload 構築 + 妥当性チェック。

    Returns: (payload, external_id, warnings)
    Raises: ValueError（売価 < 振込額のとき）
    """
    warnings: list[str] = []
    if stmt.gross_sales - stmt.transfer_amount < 0:
        raise ValueError(
            f"差額が負（売価={stmt.gross_sales} < 振込額={stmt.transfer_amount}）。"
            "データ抽出ミスの可能性があります"
        )
    if stmt.purchase_total is not None and not (
        stmt.transfer_amount <= stmt.purchase_total <= stmt.gross_sales
    ):
        warnings.append("仕入額合計が売価と振込額の範囲外")
    payload = _build_journal_payload(stmt, vendor_slug)
    external_id = _build_external_id(vendor_slug, stmt.period_end)
    return payload, external_id, warnings


def register_to_freee(payload: dict, external_id: str, run_id: str) -> dict:
    """冪等性チェック → freee 登録 → mark_executed まで。

    冪等性キーは既登録済みなら `{"skipped": True, ...}` を返す。
    dry-run か本番かは FreeeClient.register_journal が `is_dry_run()` を見て判断する。
    """
    if is_executed("dept_store_invoice", external_id):
        return {
            "skipped": True,
            "reason": "already executed",
            "external_id": external_id,
        }
    with FreeeClient() as freee:
        result = freee.register_journal(
            payload, external_id=external_id, task="dept_store_invoice"
        )
    # dry-run の場合は mark_executed しない（rehearsal を本番のidempotencyに混ぜない）
    if not result.get("dry_run"):
        mark_executed(
            "dept_store_invoice",
            external_id,
            run_id,
            str(result.get("journal_id") or ""),
            "success",
        )
    return result


def _render_preview(stmt: DeptStoreStatement, payload: dict, external_id: str) -> None:
    """rich で抽出結果と仕訳プレビューを CLI に表示する。"""
    t1 = Table(title="抽出結果", show_header=True)
    t1.add_column("項目")
    t1.add_column("値", justify="right")
    t1.add_row("取引先", stmt.vendor_name)
    t1.add_row("登録番号", stmt.vendor_registration_number or "(なし)")
    t1.add_row("期間", f"{stmt.period_start} 〜 {stmt.period_end}")
    t1.add_row("発行日", str(stmt.issued_date))
    t1.add_row("お支払日", str(stmt.payment_date))
    t1.add_row("売価", f"{stmt.gross_sales:,}")
    t1.add_row(
        "仕入額合計A",
        f"{stmt.purchase_total:,}" if stmt.purchase_total is not None else "(なし)",
    )
    t1.add_row("振込額C", f"{stmt.transfer_amount:,}")
    t1.add_row("差額（支払手数料）", f"{stmt.gross_sales - stmt.transfer_amount:,}")
    console.print(t1)

    t2 = Table(
        title=(
            f"freee仕訳プレビュー (issue_date={payload['issue_date']}, "
            f"external_id={external_id})"
        ),
        show_header=True,
    )
    t2.add_column("勘定科目ID")
    t2.add_column("方向")
    t2.add_column("金額", justify="right")
    t2.add_column("摘要")
    for d in payload["details"]:
        t2.add_row(
            str(d["account_item_id"]),
            d["entry_side"],
            f"{d['amount']:,}",
            d["description"],
        )
    console.print(t2)


def run(
    image_paths: list[Path],
    vendor_slug: str,
    run_id: Optional[str] = None,
) -> RunReport:
    init_db()
    log = get_logger("dept_store_invoice")
    run_id = run_id or generate_run_id("dept-store-invoice")
    bind_run("dept_store_invoice", run_id)
    report = RunReport(task="dept_store_invoice", run_id=run_id)
    log.info(
        "dept_store.start",
        vendor=vendor_slug,
        num_images=len(image_paths),
        dry_run=is_dry_run(),
    )

    try:
        stmt = extract_statement(image_paths)
        log.info("dept_store.extracted", **stmt.model_dump(mode="json"))

        try:
            payload, external_id, warnings = build_journal_with_validation(stmt, vendor_slug)
        except ValueError:
            raise
        for w in warnings:
            log.warning("dept_store.consistency_warning", reason=w)
            report.add_warning("consistency", w)

        if is_executed("dept_store_invoice", external_id):
            log.warning("dept_store.skip_duplicate", external_id=external_id)
            report.add_warning(external_id, "already executed")
            report.finalize()
            return report

        _render_preview(stmt, payload, external_id)

        if is_dry_run():
            console.print(
                "[yellow]dry-run モードのため登録はスキップします。"
                "本番登録には --no-dry-run を指定して再実行してください。[/yellow]"
            )
            report.add_success(external_id, "dry-run preview ok")
            report.finalize()
            return report

        try:
            answer = input(
                "\nこの内容でfreeeに登録します。よろしいですか？ [y/N]: "
            ).strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("y", "yes"):
            log.info("dept_store.aborted_by_user")
            report.add_warning(external_id, "aborted by user")
            report.finalize()
            return report

        result = register_to_freee(payload, external_id, run_id)
        log.info("dept_store.registered", external_id=external_id, result=result)
        report.add_success(external_id, "registered to freee")
    except Exception as e:
        log.exception("dept_store.failed", error=str(e))
        report.add_failure("extraction_or_registration", str(e))
        if not is_dry_run():
            notify_failure(
                "dept_store_invoice",
                run_id,
                e,
                {
                    "vendor": vendor_slug,
                    "images": [str(p) for p in image_paths],
                },
            )
    finally:
        unbind_run()

    report.finalize()
    return report
