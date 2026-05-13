"""月次在庫評価仕訳タスク。

coffee_system から当月末の在庫評価額（JPY、事業所全体合計）を取得し、
freee に振替伝票として登録する。

仕訳パターン（前月逆仕訳 + 当月計上、2本/月）:
- 月初 (YYYY-MM-01): 前月計上の取消
    (借) 期末商品棚卸高 prev_amount / (貸) 商品 prev_amount
- 月末 (YYYY-MM-末日): 当月計上
    (借) 商品 current_amount / (貸) 期末商品棚卸高 current_amount

初回実行（前月の inventory_valuations 履歴がない場合）は当月計上 1 本のみ。

冪等性:
- executed_operations: task="inventory_valuation", external_id="YYYY-MM"
- 履歴: inventory_valuations テーブル（月キーで upsert）
"""
from __future__ import annotations

import calendar
from datetime import date
from typing import Any, Optional

from rich.console import Console
from rich.table import Table

from accounting.config import settings
from accounting.connectors.coffee_system import CoffeeSystemClient
from accounting.connectors.freee import FreeeClient
from accounting.core import inventory_valuations as iv_store
from accounting.core.db import init_db
from accounting.core.dry_run import is_dry_run
from accounting.core.idempotency import is_executed, mark_executed
from accounting.core.logger import bind_run, get_logger, unbind_run
from accounting.core.notifier import notify_failure
from accounting.core.report import RunReport, generate_run_id

console = Console()

# freee マスタから引き当てる勘定科目名（ID は実行時に解決する）
ACCOUNT_NAME_INVENTORY = "商品"
ACCOUNT_NAME_CLOSING_INVENTORY = "期末商品棚卸高"

TASK_NAME = "inventory_valuation"


# ---- 純粋関数 ----


def _parse_month(month: str) -> tuple[int, int]:
    """`2026-04` → (2026, 4)。形式不正は ValueError。"""
    if len(month) != 7 or month[4] != "-":
        raise ValueError(f"month は YYYY-MM 形式で指定してください: {month!r}")
    try:
        y, m = int(month[:4]), int(month[5:])
    except ValueError:
        raise ValueError(f"month は YYYY-MM 形式で指定してください: {month!r}")
    if not (1 <= m <= 12):
        raise ValueError(f"month の月部分が不正です: {month!r}")
    return y, m


def _last_day(year: int, month: int) -> date:
    _, last = calendar.monthrange(year, month)
    return date(year, month, last)


def _resolve_account_id(account_items: list[dict[str, Any]], name: str) -> int:
    """freee 勘定科目マスタから name で ID を引き当て。見つからなければ ValueError。"""
    for a in account_items:
        if a.get("name") == name:
            aid = a.get("id")
            if not isinstance(aid, int):
                raise ValueError(f"freee 勘定科目 '{name}' の id が integer でない: {aid!r}")
            return aid
    available = ", ".join(sorted({a.get("name") for a in account_items if a.get("name")}))
    raise ValueError(
        f"freee 勘定科目 '{name}' が見つかりません。"
        f"freee 画面で勘定科目を確認してください。利用可能（一部）: {available[:300]}"
    )


def _build_closing_payload(
    *,
    company_id: int,
    issue_date: date,
    amount: int,
    inventory_aid: int,
    closing_inventory_aid: int,
    month: str,
) -> dict[str, Any]:
    """当月計上: (借) 商品 / (貸) 期末商品棚卸高 amount。"""
    return {
        "company_id": company_id,
        "issue_date": issue_date.isoformat(),
        "details": [
            {
                "entry_side": "debit",
                "account_item_id": inventory_aid,
                "tax_code": 0,
                "amount": amount,
                "description": f"在庫評価 {month}月末計上",
            },
            {
                "entry_side": "credit",
                "account_item_id": closing_inventory_aid,
                "tax_code": 0,
                "amount": amount,
                "description": f"在庫評価 {month}月末計上",
            },
        ],
    }


def _build_reversal_payload(
    *,
    company_id: int,
    issue_date: date,
    amount: int,
    inventory_aid: int,
    closing_inventory_aid: int,
    prev_month: str,
) -> dict[str, Any]:
    """前月逆仕訳: (借) 期末商品棚卸高 / (貸) 商品 amount。"""
    return {
        "company_id": company_id,
        "issue_date": issue_date.isoformat(),
        "details": [
            {
                "entry_side": "debit",
                "account_item_id": closing_inventory_aid,
                "tax_code": 0,
                "amount": amount,
                "description": f"在庫評価 {prev_month}月末分の取消",
            },
            {
                "entry_side": "credit",
                "account_item_id": inventory_aid,
                "tax_code": 0,
                "amount": amount,
                "description": f"在庫評価 {prev_month}月末分の取消",
            },
        ],
    }


def _render_preview(
    *,
    month: str,
    amount_current: int,
    amount_prev: Optional[int],
    prev_month: str,
    closing_payload: dict[str, Any],
    reversal_payload: Optional[dict[str, Any]],
    external_id: str,
) -> None:
    t1 = Table(title="月次在庫評価サマリ", show_header=True)
    t1.add_column("項目")
    t1.add_column("値", justify="right")
    t1.add_row("対象月", month)
    t1.add_row("当月末評価額（JPY）", f"{amount_current:,}")
    t1.add_row(
        "前月末評価額（JPY）",
        f"{amount_prev:,}（{prev_month}）" if amount_prev is not None else "(履歴なし → 逆仕訳省略)",
    )
    t1.add_row("external_id", external_id)
    console.print(t1)

    if reversal_payload is not None:
        t2 = Table(
            title=f"① 前月逆仕訳 (issue_date={reversal_payload['issue_date']})",
            show_header=True,
        )
        t2.add_column("勘定科目ID")
        t2.add_column("方向")
        t2.add_column("金額", justify="right")
        t2.add_column("摘要")
        for d in reversal_payload["details"]:
            t2.add_row(
                str(d["account_item_id"]),
                d["entry_side"],
                f"{d['amount']:,}",
                d["description"],
            )
        console.print(t2)

    t3 = Table(
        title=f"② 当月計上 (issue_date={closing_payload['issue_date']})",
        show_header=True,
    )
    t3.add_column("勘定科目ID")
    t3.add_column("方向")
    t3.add_column("金額", justify="right")
    t3.add_column("摘要")
    for d in closing_payload["details"]:
        t3.add_row(
            str(d["account_item_id"]),
            d["entry_side"],
            f"{d['amount']:,}",
            d["description"],
        )
    console.print(t3)


# ---- ランナー ----


def run(
    month: str,
    amount_override: Optional[int] = None,
    run_id: Optional[str] = None,
) -> RunReport:
    """月次在庫評価仕訳の実行。

    Args:
        month: "YYYY-MM" 形式
        amount_override: 指定があれば coffee_system を呼ばずこの値を使う（緊急用）
        run_id: 省略時は自動生成
    """
    init_db()
    log = get_logger(TASK_NAME)
    run_id = run_id or generate_run_id("inventory-valuation")
    bind_run(TASK_NAME, run_id)
    report = RunReport(task=TASK_NAME, run_id=run_id)

    log.info("inventory_valuation.start", month=month, dry_run=is_dry_run())

    try:
        year, mon = _parse_month(month)
        external_id = month  # YYYY-MM
        prev_month = iv_store.previous_month_key(month)

        if is_executed(TASK_NAME, external_id):
            log.warning("inventory_valuation.skip_duplicate", external_id=external_id)
            report.add_warning(external_id, "already executed")
            report.finalize()
            return report

        # 1. 当月評価額の取得
        if amount_override is not None:
            current_amount = int(amount_override)
            as_of = _last_day(year, mon)
            log.info(
                "inventory_valuation.amount_override",
                amount=current_amount,
                as_of=str(as_of),
            )
        else:
            with CoffeeSystemClient() as cs:
                data = cs.get_inventory_value()
            current_amount = int(data.get("total_jpy") or 0)
            as_of_str = data.get("as_of")
            as_of = date.fromisoformat(as_of_str) if as_of_str else _last_day(year, mon)
            if current_amount <= 0:
                raise ValueError(
                    f"coffee_system が返した評価額が 0 以下: {current_amount}（month={month}）"
                )

        # 2. 前月評価額の取得（履歴があれば）
        prev_record = iv_store.get_by_month(prev_month)
        prev_amount: Optional[int] = (
            int(prev_record["amount_jpy"]) if prev_record is not None else None
        )
        if prev_amount is None:
            log.info(
                "inventory_valuation.no_previous_record",
                prev_month=prev_month,
                note="前月逆仕訳をスキップして当月計上のみ実施",
            )
            report.add_warning(prev_month, "no previous valuation; reversal skipped")

        # 3. 勘定科目 ID 解決（freee マスタから引き当て）
        if not settings.freee_company_id:
            raise ValueError("FREEE_COMPANY_ID が未設定です")
        company_id = int(settings.freee_company_id)
        with FreeeClient() as freee:
            account_items = freee.get_account_items()
        inventory_aid = _resolve_account_id(account_items, ACCOUNT_NAME_INVENTORY)
        closing_inventory_aid = _resolve_account_id(
            account_items, ACCOUNT_NAME_CLOSING_INVENTORY
        )
        log.info(
            "inventory_valuation.account_ids_resolved",
            inventory_aid=inventory_aid,
            closing_inventory_aid=closing_inventory_aid,
        )

        # 4. 仕訳 payload 構築
        closing_payload = _build_closing_payload(
            company_id=company_id,
            issue_date=_last_day(year, mon),
            amount=current_amount,
            inventory_aid=inventory_aid,
            closing_inventory_aid=closing_inventory_aid,
            month=month,
        )
        reversal_payload: Optional[dict[str, Any]] = None
        if prev_amount is not None:
            reversal_payload = _build_reversal_payload(
                company_id=company_id,
                issue_date=date(year, mon, 1),
                amount=prev_amount,
                inventory_aid=inventory_aid,
                closing_inventory_aid=closing_inventory_aid,
                prev_month=prev_month,
            )

        _render_preview(
            month=month,
            amount_current=current_amount,
            amount_prev=prev_amount,
            prev_month=prev_month,
            closing_payload=closing_payload,
            reversal_payload=reversal_payload,
            external_id=external_id,
        )

        if is_dry_run():
            console.print(
                "[yellow]dry-run モードのため freee には登録しません。"
                "本番登録には --no-dry-run を指定してください。[/yellow]"
            )
            report.add_success(external_id, "dry-run preview ok")
            report.finalize()
            return report

        # 5. 確認プロンプト
        try:
            answer = input(
                "\nこの内容で freee に登録します。よろしいですか？ [y/N]: "
            ).strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("y", "yes"):
            log.info("inventory_valuation.aborted_by_user")
            report.add_warning(external_id, "aborted by user")
            report.finalize()
            return report

        # 6. freee 登録
        journal_id_reversal: Optional[str] = None
        journal_id_closing: Optional[str] = None
        with FreeeClient() as freee:
            if reversal_payload is not None:
                r1 = freee.create_manual_journal(
                    reversal_payload,
                    external_id=f"{external_id}-reversal",
                    task=TASK_NAME,
                )
                journal_id_reversal = (
                    str(r1.get("manual_journal_id")) if r1.get("manual_journal_id") else None
                )
                log.info(
                    "inventory_valuation.reversal_registered",
                    manual_journal_id=journal_id_reversal,
                )
            r2 = freee.create_manual_journal(
                closing_payload,
                external_id=f"{external_id}-closing",
                task=TASK_NAME,
            )
            journal_id_closing = (
                str(r2.get("manual_journal_id")) if r2.get("manual_journal_id") else None
            )
            log.info(
                "inventory_valuation.closing_registered",
                manual_journal_id=journal_id_closing,
            )

        # 7. 履歴と冪等性のマーク
        iv_store.upsert(
            month=month,
            amount_jpy=current_amount,
            as_of=as_of,
            run_id=run_id,
            journal_id_closing=journal_id_closing,
            journal_id_reversal=journal_id_reversal,
        )
        mark_executed(
            TASK_NAME,
            external_id,
            run_id,
            journal_id_closing or "",
            "success",
        )

        report.add_success(external_id, "registered to freee")
    except Exception as e:
        log.exception("inventory_valuation.failed", error=str(e))
        report.add_failure(month, str(e))
        if not is_dry_run():
            notify_failure(
                TASK_NAME,
                run_id,
                e,
                {"month": month, "amount_override": amount_override},
            )
    finally:
        unbind_run()

    report.finalize()
    return report
