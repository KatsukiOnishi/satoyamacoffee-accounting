"""月次在庫評価仕訳 Web UI ルート。

役割:
- 対象月を選択
- coffee_system から当月評価額を取得 or 手入力
- 振替伝票 payload をプレビュー（前月逆仕訳 + 当月計上）
- 確認後に freee に登録（manual_journal × 2本）

CLI の `accounting.tasks.inventory_valuation.run()` は内部で確認プロンプト
（input()）を持つため Web からは使えない。本ルートは同タスクの純粋関数を
直接呼んで Web フローに組み込む。
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from accounting.config import settings
from accounting.connectors.coffee_system import CoffeeSystemClient
from accounting.connectors.freee import FreeeClient
from accounting.core import inventory_valuations as iv_store
from accounting.core.db import init_db
from accounting.core.dry_run import DryRunContext
from accounting.core.idempotency import is_executed, mark_executed
from accounting.core.logger import bind_run, get_logger, unbind_run
from accounting.core.report import generate_run_id
from accounting.tasks.inventory_valuation import (
    ACCOUNT_NAME_CLOSING_INVENTORY,
    ACCOUNT_NAME_INVENTORY,
    TASK_NAME,
    _build_closing_payload,
    _build_reversal_payload,
    _last_day,
    _parse_month,
    _resolve_account_id,
)
from accounting.web.app import templates

router = APIRouter(prefix="/tasks/inventory-valuation")
log = get_logger("web.inventory_valuation")


def _default_month() -> str:
    """今日の前月を既定値として返す。月初は前月の月次決算を回す想定。"""
    today = date.today()
    y, m = today.year, today.month - 1
    if m == 0:
        m = 12
        y -= 1
    return f"{y:04d}-{m:02d}"


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def form(request: Request) -> HTMLResponse:
    init_db()
    # 直近の履歴を表示
    return templates.TemplateResponse(
        request,
        "tasks/inventory_valuation/form.html",
        {
            "default_month": _default_month(),
            "coffee_system_url": settings.coffee_system_base_url,
            "dry_run": settings.dry_run,
        },
    )


@router.post("/preview", response_class=HTMLResponse)
async def preview(
    request: Request,
    month: str = Form(...),
    amount_override: str = Form(default=""),
    dry_run: str | None = Form(default=None),
) -> HTMLResponse:
    init_db()
    is_dry = dry_run is not None
    try:
        year, mon = _parse_month(month)
    except ValueError as e:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": str(e), "dry_run": is_dry},
            status_code=400,
        )

    external_id = month
    prev_month = iv_store.previous_month_key(month)
    already = is_executed(TASK_NAME, external_id)

    # 当月評価額
    try:
        if amount_override.strip():
            current_amount = int(amount_override.replace(",", "").strip())
            if current_amount <= 0:
                raise ValueError("評価額は正の整数で入力してください")
            as_of = _last_day(year, mon)
            source = "manual_input"
        else:
            with CoffeeSystemClient() as cs:
                data = cs.get_inventory_value()
            current_amount = int(data.get("total_jpy") or 0)
            as_of_str = data.get("as_of")
            as_of = date.fromisoformat(as_of_str) if as_of_str else _last_day(year, mon)
            source = "coffee_system"
            if current_amount <= 0:
                raise ValueError(f"coffee_system が返した評価額が 0 以下: {current_amount}")
    except Exception as e:
        log.exception("web.inventory_valuation.amount_fetch_failed")
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": f"評価額の取得に失敗: {e}", "dry_run": is_dry},
            status_code=500,
        )

    # 前月履歴
    prev_record = iv_store.get_by_month(prev_month)
    prev_amount = int(prev_record["amount_jpy"]) if prev_record is not None else None

    # 勘定科目 ID 引き当て
    try:
        if not settings.freee_company_id:
            raise ValueError("FREEE_COMPANY_ID が未設定です")
        company_id = int(settings.freee_company_id)
        with FreeeClient() as freee:
            account_items = freee.get_account_items()
        inventory_aid = _resolve_account_id(account_items, ACCOUNT_NAME_INVENTORY)
        closing_inventory_aid = _resolve_account_id(
            account_items, ACCOUNT_NAME_CLOSING_INVENTORY
        )
    except Exception as e:
        log.exception("web.inventory_valuation.account_resolve_failed")
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": f"勘定科目の引き当てに失敗: {e}", "dry_run": is_dry},
            status_code=500,
        )

    # payload 構築
    closing_payload = _build_closing_payload(
        company_id=company_id,
        issue_date=_last_day(year, mon),
        amount=current_amount,
        inventory_aid=inventory_aid,
        closing_inventory_aid=closing_inventory_aid,
        month=month,
    )
    reversal_payload = None
    if prev_amount is not None:
        reversal_payload = _build_reversal_payload(
            company_id=company_id,
            issue_date=date(year, mon, 1),
            amount=prev_amount,
            inventory_aid=inventory_aid,
            closing_inventory_aid=closing_inventory_aid,
            prev_month=prev_month,
        )

    return templates.TemplateResponse(
        request,
        "tasks/inventory_valuation/preview.html",
        {
            "month": month,
            "prev_month": prev_month,
            "current_amount": current_amount,
            "prev_amount": prev_amount,
            "as_of": as_of,
            "source": source,
            "closing_payload": closing_payload,
            "reversal_payload": reversal_payload,
            "external_id": external_id,
            "already_registered": already,
            "dry_run": is_dry,
        },
    )


@router.post("/register", response_class=HTMLResponse)
async def register(
    request: Request,
    month: str = Form(...),
    amount: int = Form(...),
    dry_run: str | None = Form(default=None),
) -> HTMLResponse:
    """preview で得た値で本登録する。

    amount は preview から hidden で渡される（再度 coffee_system を呼ばないため）。
    """
    init_db()
    is_dry = dry_run is not None
    try:
        year, mon = _parse_month(month)
    except ValueError as e:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": str(e), "dry_run": is_dry},
            status_code=400,
        )

    external_id = month
    if is_executed(TASK_NAME, external_id):
        return templates.TemplateResponse(
            request,
            "tasks/inventory_valuation/result.html",
            {
                "status": "skipped",
                "month": month,
                "external_id": external_id,
                "journal_id_closing": None,
                "journal_id_reversal": None,
                "error": None,
                "dry_run": is_dry,
            },
        )

    prev_month = iv_store.previous_month_key(month)
    prev_record = iv_store.get_by_month(prev_month)
    prev_amount = int(prev_record["amount_jpy"]) if prev_record is not None else None
    as_of = _last_day(year, mon)

    run_id = generate_run_id("inventory-valuation")
    bind_run(TASK_NAME, run_id)
    try:
        if not settings.freee_company_id:
            raise ValueError("FREEE_COMPANY_ID が未設定です")
        company_id = int(settings.freee_company_id)
        with DryRunContext(is_dry):
            with FreeeClient() as freee:
                account_items = freee.get_account_items()
                inventory_aid = _resolve_account_id(account_items, ACCOUNT_NAME_INVENTORY)
                closing_inventory_aid = _resolve_account_id(
                    account_items, ACCOUNT_NAME_CLOSING_INVENTORY
                )

                journal_id_reversal: str | None = None
                journal_id_closing: str | None = None

                if prev_amount is not None:
                    reversal_payload = _build_reversal_payload(
                        company_id=company_id,
                        issue_date=date(year, mon, 1),
                        amount=prev_amount,
                        inventory_aid=inventory_aid,
                        closing_inventory_aid=closing_inventory_aid,
                        prev_month=prev_month,
                    )
                    r1 = freee.create_manual_journal(
                        reversal_payload,
                        external_id=f"{external_id}-reversal",
                        task=TASK_NAME,
                    )
                    journal_id_reversal = (
                        str(r1.get("manual_journal_id"))
                        if r1.get("manual_journal_id")
                        else None
                    )

                closing_payload = _build_closing_payload(
                    company_id=company_id,
                    issue_date=as_of,
                    amount=amount,
                    inventory_aid=inventory_aid,
                    closing_inventory_aid=closing_inventory_aid,
                    month=month,
                )
                r2 = freee.create_manual_journal(
                    closing_payload,
                    external_id=f"{external_id}-closing",
                    task=TASK_NAME,
                )
                journal_id_closing = (
                    str(r2.get("manual_journal_id"))
                    if r2.get("manual_journal_id")
                    else None
                )

            if not is_dry:
                iv_store.upsert(
                    month=month,
                    amount_jpy=amount,
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
    except Exception as e:
        log.exception("web.inventory_valuation.register_failed")
        return templates.TemplateResponse(
            request,
            "tasks/inventory_valuation/result.html",
            {
                "status": "failed",
                "month": month,
                "external_id": external_id,
                "journal_id_closing": None,
                "journal_id_reversal": None,
                "error": str(e),
                "dry_run": is_dry,
            },
            status_code=500,
        )
    finally:
        unbind_run()

    return templates.TemplateResponse(
        request,
        "tasks/inventory_valuation/result.html",
        {
            "status": "dry_run" if is_dry else "success",
            "month": month,
            "external_id": external_id,
            "journal_id_closing": journal_id_closing if not is_dry else None,
            "journal_id_reversal": journal_id_reversal if not is_dry else None,
            "error": None,
            "dry_run": is_dry,
        },
    )
