"""月次給与仕訳 Web UI ルート。

フロー:
  GET  /tasks/payroll                  → 月選択フォーム
  POST /tasks/payroll/preview          → attendance-system から取得 + 仕訳プレビュー
  POST /tasks/payroll/register         → freee に各社員の振替伝票を登録

CLI の `accounting.tasks.payroll.run()` は対話プロンプトを持つため、Web からは
純粋関数を直接呼んで Web 用フローを組む。
"""
from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from accounting.config import settings
from accounting.connectors.attendance import AttendanceClient, SalaryRow
from accounting.connectors.freee import FreeeClient
from accounting.core.db import init_db
from accounting.core.dry_run import DryRunContext
from accounting.core.idempotency import is_executed, mark_executed
from accounting.core.logger import bind_run, get_logger, unbind_run
from accounting.core.report import generate_run_id
from accounting.tasks.payroll import (
    TASK_NAME,
    _last_day,
    _parse_month,
    build_external_id,
    build_journal_payload,
    resolve_account_ids,
)
from accounting.web.app import templates

router = APIRouter(prefix="/tasks/payroll")
log = get_logger("web.payroll")


def _default_month() -> str:
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
    return templates.TemplateResponse(
        request,
        "tasks/payroll/form.html",
        {
            "default_month": _default_month(),
            "attendance_url": settings.attendance_system_base_url,
            "dry_run": settings.dry_run,
        },
    )


@router.post("/preview", response_class=HTMLResponse)
async def preview(
    request: Request,
    month: str = Form(...),
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

    issue_date = _last_day(year, mon)

    # 1. attendance-system から取得
    try:
        with AttendanceClient() as att:
            rows = att.get_salaries(year=year, month=mon)
    except Exception as e:
        log.exception("web.payroll.fetch_failed")
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "message": f"attendance-system からの取得に失敗: {e}",
                "dry_run": is_dry,
            },
            status_code=500,
        )

    if not rows:
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "message": f"{month} の給与データが0件です。attendance-system 側で給与計算を完了させてから再実行してください。",
                "dry_run": is_dry,
            },
            status_code=404,
        )

    # 2. 勘定科目 ID
    try:
        if not settings.freee_company_id:
            raise ValueError("FREEE_COMPANY_ID が未設定")
        company_id = int(settings.freee_company_id)
        with FreeeClient() as freee:
            account_items = freee.get_account_items()
        ids = resolve_account_ids(account_items)
    except Exception as e:
        log.exception("web.payroll.account_resolve_failed")
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": f"勘定科目の引き当てに失敗: {e}", "dry_run": is_dry},
            status_code=500,
        )

    # 3. 各社員 payload
    entries: list[dict] = []
    totals = {
        "base_pay": 0, "transport_pay": 0,
        "income_tax": 0, "resident_tax": 0, "social_ins": 0,
        "net_pay": 0,
    }
    for r in rows:
        ext_id = build_external_id(year, mon, r.staff_id)
        try:
            payload = build_journal_payload(
                row=r,
                account_ids=ids,
                company_id=company_id,
                issue_date=issue_date,
            )
        except ValueError as e:
            entries.append({
                "row": r,
                "external_id": ext_id,
                "payload": None,
                "already_registered": False,
                "error": str(e),
            })
            continue
        entries.append({
            "row": r,
            "external_id": ext_id,
            "payload": payload,
            "already_registered": is_executed(TASK_NAME, ext_id),
            "error": None,
        })
        totals["base_pay"] += r.base_pay
        totals["transport_pay"] += r.transport_pay
        totals["income_tax"] += r.income_tax
        totals["resident_tax"] += r.resident_tax
        totals["social_ins"] += r.social_ins
        totals["net_pay"] += r.net_pay

    # シリアライズ（POST register に渡す）
    payload_bundle = {
        "month": month,
        "company_id": company_id,
        "issue_date": issue_date.isoformat(),
        "account_ids": {
            "salary": ids.salary,
            "transport": ids.transport,
            "deposit": ids.deposit,
            "payables": ids.payables,
        },
        "rows": [
            {
                "staff_id": r.staff_id,
                "employee_no": r.employee_no,
                "last_name": r.last_name,
                "first_name": r.first_name,
                "department": r.department,
                "work_days": r.work_days,
                "work_min": r.work_min,
                "base_pay": r.base_pay,
                "transport_pay": r.transport_pay,
                "income_tax": r.income_tax,
                "resident_tax": r.resident_tax,
                "social_ins": r.social_ins,
                "total_pay": r.total_pay,
                "total_deduct": r.total_deduct,
                "net_pay": r.net_pay,
            }
            for r in rows
        ],
    }

    return templates.TemplateResponse(
        request,
        "tasks/payroll/preview.html",
        {
            "month": month,
            "issue_date": issue_date,
            "entries": entries,
            "totals": totals,
            "ids": ids,
            "payload_bundle_json": json.dumps(payload_bundle, ensure_ascii=False),
            "dry_run": is_dry,
        },
    )


@router.post("/register", response_class=HTMLResponse)
async def register(
    request: Request,
    payload_bundle: str = Form(...),
    dry_run: str | None = Form(default=None),
) -> HTMLResponse:
    init_db()
    is_dry = dry_run is not None
    try:
        bundle = json.loads(payload_bundle)
    except Exception:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "payload_bundle のパースに失敗。プレビューからやり直してください。", "dry_run": is_dry},
            status_code=400,
        )

    month = bundle["month"]
    year, mon = _parse_month(month)
    issue_date = date.fromisoformat(bundle["issue_date"])
    company_id = int(bundle["company_id"])
    aids = bundle["account_ids"]

    from accounting.tasks.payroll import AccountIdSet

    ids = AccountIdSet(
        salary=int(aids["salary"]),
        transport=int(aids["transport"]),
        deposit=int(aids["deposit"]),
        payables=int(aids["payables"]),
    )

    run_id = generate_run_id("payroll")
    bind_run(TASK_NAME, run_id)

    successes: list[dict] = []
    skipped: list[dict] = []
    failures: list[dict] = []

    try:
        with DryRunContext(is_dry):
            with FreeeClient() as freee:
                for raw in bundle["rows"]:
                    r = SalaryRow.from_dict({
                        "staffId": raw["staff_id"],
                        "employeeNo": raw["employee_no"],
                        "lastName": raw["last_name"],
                        "firstName": raw["first_name"],
                        "department": raw["department"],
                        "workDays": raw["work_days"],
                        "workMin": raw["work_min"],
                        "basePay": raw["base_pay"],
                        "transportPay": raw["transport_pay"],
                        "incomeTax": raw["income_tax"],
                        "residentTax": raw["resident_tax"],
                        "socialIns": raw["social_ins"],
                        "totalPay": raw["total_pay"],
                        "totalDeduct": raw["total_deduct"],
                        "netPay": raw["net_pay"],
                    })
                    ext_id = build_external_id(year, mon, r.staff_id)

                    if is_executed(TASK_NAME, ext_id):
                        skipped.append({"name": r.full_name, "external_id": ext_id, "reason": "already_executed"})
                        continue

                    try:
                        payload = build_journal_payload(
                            row=r,
                            account_ids=ids,
                            company_id=company_id,
                            issue_date=issue_date,
                        )
                        result = freee.create_manual_journal(
                            payload, external_id=ext_id, task=TASK_NAME
                        )
                        if result.get("dry_run"):
                            successes.append({
                                "name": r.full_name,
                                "external_id": ext_id,
                                "manual_journal_id": None,
                                "dry_run": True,
                            })
                            continue
                        mj_id = result.get("manual_journal_id")
                        mark_executed(TASK_NAME, ext_id, run_id, str(mj_id or ""), "success")
                        successes.append({
                            "name": r.full_name,
                            "external_id": ext_id,
                            "manual_journal_id": mj_id,
                            "dry_run": False,
                        })
                    except Exception as e:
                        log.exception("web.payroll.register_failed", external_id=ext_id)
                        failures.append({
                            "name": r.full_name,
                            "external_id": ext_id,
                            "error": str(e),
                        })
    finally:
        unbind_run()

    return templates.TemplateResponse(
        request,
        "tasks/payroll/result.html",
        {
            "month": month,
            "successes": successes,
            "skipped": skipped,
            "failures": failures,
            "dry_run": is_dry,
        },
    )
