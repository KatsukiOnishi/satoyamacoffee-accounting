"""月次給与仕訳タスク。

attendance-system から月次給与データ（社員別）を取得し、freee に
**社員1人あたり1本の振替伝票**として登録する。

仕訳パターン（1社員1月分）:
  借方:
    給料手当         basePay
    旅費交通費       transportPay  (>0 のときのみ)
  貸方:
    預り金           incomeTax     description="所得税預り" (>0)
    預り金           residentTax   description="住民税預り" (>0)
    預り金           socialIns     description="社会保険料預り" (>0)
    未払金           netPay        description="給与未払（差引支給）"

issue_date は対象月の月末。**直接 PayPay銀行で立てない** のは、後日の銀行同期
（freee の「自動で経理」）で同じ出金を拾った時に、ユーザーが「未払金」勘定で
取引承認するだけで自動消し込みできるようにするため（vendor-invoice と同じ思想）。

社会保険の **会社負担分（法定福利費）は本タスクの対象外** — 別途手動 or 後続タスクで対応。

冪等性:
- executed_operations: task="payroll", external_id="payroll-YYYY-MM-{staffId}"
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from rich.console import Console
from rich.table import Table

from accounting.config import settings
from accounting.connectors.attendance import AttendanceClient, SalaryRow
from accounting.connectors.freee import FreeeClient
from accounting.core.db import init_db
from accounting.core.dry_run import is_dry_run
from accounting.core.idempotency import is_executed, mark_executed
from accounting.core.logger import bind_run, get_logger, unbind_run
from accounting.core.notifier import notify_failure
from accounting.core.report import RunReport, generate_run_id

console = Console()
log = get_logger("payroll")

TASK_NAME = "payroll"

# freee 勘定科目（名前で引き当て、ID はハードコードしない）。
# 事業所により表記が違うので、必要なら env で上書き可能にする。
ACCOUNT_NAME_SALARY = "給料手当"
ACCOUNT_NAME_TRANSPORT = "旅費交通費"
ACCOUNT_NAME_DEPOSIT = "預り金"
# 給与の差引支給額は「未払金」で立てる（B方式）。後日 freee が銀行同期で振込を
# 拾った際、ユーザーが freee「自動で経理」で同 amount の「未払金」取引として承認
# すれば自動消し込みされる。vendor-invoice と同じ運用思想。
ACCOUNT_NAME_PAYABLES = "未払金"


@dataclass
class AccountIdSet:
    salary: int
    transport: int
    deposit: int
    payables: int


# ---- 純粋関数 ----


def _parse_month(month: str) -> tuple[int, int]:
    if len(month) != 7 or month[4] != "-":
        raise ValueError(f"month は YYYY-MM 形式で指定: {month!r}")
    try:
        y, m = int(month[:4]), int(month[5:])
    except ValueError:
        raise ValueError(f"month は YYYY-MM 形式で指定: {month!r}")
    if not (1 <= m <= 12):
        raise ValueError(f"month の月部分が不正: {month!r}")
    return y, m


def _last_day(year: int, month: int) -> date:
    _, last = calendar.monthrange(year, month)
    return date(year, month, last)


def resolve_account_ids(account_items: list[dict[str, Any]]) -> AccountIdSet:
    """freee 勘定科目マスタから給与仕訳に必要な ID を引き当てる。

    完全一致を優先、無ければ「名前を含む」で部分一致を許容。
    どれかが見つからなければ ValueError。
    """
    name_to_id: dict[str, int] = {}
    for a in account_items:
        n = a.get("name")
        i = a.get("id")
        if n and isinstance(i, int):
            name_to_id[n] = i

    def find(target: str) -> int:
        if target in name_to_id:
            return name_to_id[target]
        for n, i in name_to_id.items():
            if target in n or n in target:
                return i
        raise ValueError(
            f"freee 勘定科目 '{target}' が見つかりません。"
            "freee 画面で勘定科目を作成してください。"
        )

    return AccountIdSet(
        salary=find(ACCOUNT_NAME_SALARY),
        transport=find(ACCOUNT_NAME_TRANSPORT),
        deposit=find(ACCOUNT_NAME_DEPOSIT),
        payables=find(ACCOUNT_NAME_PAYABLES),
    )


def build_external_id(year: int, month: int, staff_id: str) -> str:
    return f"payroll-{year:04d}-{month:02d}-{staff_id}"


def build_journal_payload(
    *,
    row: SalaryRow,
    account_ids: AccountIdSet,
    company_id: int,
    issue_date: date,
) -> dict[str, Any]:
    """1社員1月分の振替伝票 payload を組み立てる。借方 = 貸方が成り立つことを保証。"""
    desc_prefix = f"{row.last_name}{row.first_name} {issue_date.year}-{issue_date.month:02d}月給与"

    details: list[dict[str, Any]] = []
    # ---- 借方 ----
    details.append(
        {
            "entry_side": "debit",
            "account_item_id": account_ids.salary,
            "tax_code": 0,
            "amount": row.base_pay,
            "description": f"{desc_prefix} 基本給",
        }
    )
    if row.transport_pay > 0:
        details.append(
            {
                "entry_side": "debit",
                "account_item_id": account_ids.transport,
                "tax_code": 0,
                "amount": row.transport_pay,
                "description": f"{desc_prefix} 交通費",
            }
        )

    # ---- 貸方 ----
    if row.income_tax > 0:
        details.append(
            {
                "entry_side": "credit",
                "account_item_id": account_ids.deposit,
                "tax_code": 0,
                "amount": row.income_tax,
                "description": f"{desc_prefix} 所得税預り",
            }
        )
    if row.resident_tax > 0:
        details.append(
            {
                "entry_side": "credit",
                "account_item_id": account_ids.deposit,
                "tax_code": 0,
                "amount": row.resident_tax,
                "description": f"{desc_prefix} 住民税預り",
            }
        )
    if row.social_ins > 0:
        details.append(
            {
                "entry_side": "credit",
                "account_item_id": account_ids.deposit,
                "tax_code": 0,
                "amount": row.social_ins,
                "description": f"{desc_prefix} 社会保険料預り",
            }
        )
    details.append(
        {
            "entry_side": "credit",
            "account_item_id": account_ids.payables,
            "tax_code": 0,
            "amount": row.net_pay,
            "description": f"{desc_prefix} 差引支給未払",
        }
    )

    # 借方=貸方 チェック
    debits = sum(d["amount"] for d in details if d["entry_side"] == "debit")
    credits = sum(d["amount"] for d in details if d["entry_side"] == "credit")
    if debits != credits:
        raise ValueError(
            f"借方({debits}) と 貸方({credits}) が一致しません: staff={row.full_name} row={row}"
        )

    return {
        "company_id": company_id,
        "issue_date": issue_date.isoformat(),
        "details": details,
    }


# ---- ランナー ----


def _render_preview(
    year: int,
    month: int,
    rows: list[SalaryRow],
    payloads: list[tuple[SalaryRow, dict[str, Any], str]],
) -> None:
    """rich で社員別給与とフレ仕訳プレビューを表示する。"""
    t1 = Table(title=f"{year}-{month:02d} 月次給与サマリ ({len(rows)} 名)", show_header=True)
    t1.add_column("社員")
    t1.add_column("基本給", justify="right")
    t1.add_column("交通費", justify="right")
    t1.add_column("所得税", justify="right")
    t1.add_column("住民税", justify="right")
    t1.add_column("社保", justify="right")
    t1.add_column("差引", justify="right")
    for r in rows:
        t1.add_row(
            r.full_name,
            f"{r.base_pay:,}",
            f"{r.transport_pay:,}",
            f"{r.income_tax:,}",
            f"{r.resident_tax:,}",
            f"{r.social_ins:,}",
            f"{r.net_pay:,}",
        )
    # 合計行
    t1.add_row(
        "[bold]合計[/bold]",
        f"{sum(r.base_pay for r in rows):,}",
        f"{sum(r.transport_pay for r in rows):,}",
        f"{sum(r.income_tax for r in rows):,}",
        f"{sum(r.resident_tax for r in rows):,}",
        f"{sum(r.social_ins for r in rows):,}",
        f"{sum(r.net_pay for r in rows):,}",
    )
    console.print(t1)


def run(month: str, run_id: Optional[str] = None) -> RunReport:
    """月次給与仕訳の実行。

    Args:
        month: "YYYY-MM"
        run_id: 省略時は自動生成
    """
    init_db()
    run_id = run_id or generate_run_id("payroll")
    bind_run(TASK_NAME, run_id)
    report = RunReport(task=TASK_NAME, run_id=run_id)
    log.info("payroll.start", month=month, dry_run=is_dry_run())

    try:
        year, mon = _parse_month(month)
        issue_date = _last_day(year, mon)

        # 1. 給与データ取得
        with AttendanceClient() as att:
            rows = att.get_salaries(year=year, month=mon)
        if not rows:
            log.warning("payroll.no_salaries", month=month)
            report.add_warning(month, "no_salaries_for_month")
            report.finalize()
            return report

        # 2. 勘定科目 ID 引き当て
        if not settings.freee_company_id:
            raise ValueError("FREEE_COMPANY_ID が未設定です")
        company_id = int(settings.freee_company_id)
        with FreeeClient() as freee:
            account_items = freee.get_account_items()
        ids = resolve_account_ids(account_items)
        log.info(
            "payroll.account_ids_resolved",
            salary=ids.salary,
            transport=ids.transport,
            deposit=ids.deposit,
            bank=ids.bank,
        )

        # 3. 各社員 payload 構築
        payloads: list[tuple[SalaryRow, dict[str, Any], str]] = []
        for r in rows:
            ext_id = build_external_id(year, mon, r.staff_id)
            payload = build_journal_payload(
                row=r,
                account_ids=ids,
                company_id=company_id,
                issue_date=issue_date,
            )
            payloads.append((r, payload, ext_id))

        _render_preview(year, mon, rows, payloads)

        if is_dry_run():
            console.print(
                "[yellow]dry-run モードのため freee には登録しません。"
                "本番登録には --no-dry-run を指定してください。[/yellow]"
            )
            for r, _payload, ext_id in payloads:
                report.add_success(ext_id, f"dry-run preview ok ({r.full_name})")
            report.finalize()
            return report

        # 4. 確認プロンプト
        try:
            answer = input(
                f"\n{len(payloads)} 名の給与を freee に登録します。よろしいですか？ [y/N]: "
            ).strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("y", "yes"):
            log.info("payroll.aborted_by_user")
            report.add_warning(month, "aborted by user")
            report.finalize()
            return report

        # 5. 登録
        with FreeeClient() as freee:
            for r, payload, ext_id in payloads:
                if is_executed(TASK_NAME, ext_id):
                    log.warning("payroll.skip_duplicate", external_id=ext_id)
                    report.add_warning(ext_id, "already executed")
                    continue
                try:
                    result = freee.create_manual_journal(
                        payload, external_id=ext_id, task=TASK_NAME
                    )
                    journal_id = result.get("manual_journal_id")
                    mark_executed(
                        TASK_NAME,
                        ext_id,
                        run_id,
                        str(journal_id or ""),
                        "success",
                    )
                    report.add_success(ext_id, f"registered: {r.full_name}")
                except Exception as e:
                    log.exception("payroll.register_failed", external_id=ext_id)
                    report.add_failure(ext_id, str(e))
    except Exception as e:
        log.exception("payroll.failed", error=str(e))
        report.add_failure(month, str(e))
        if not is_dry_run():
            notify_failure(TASK_NAME, run_id, e, {"month": month})
    finally:
        unbind_run()

    report.finalize()
    return report
