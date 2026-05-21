"""vendor-invoice Web UI ルート。

役割:
- 候補テーブル `vendor_invoice_candidates` の閲覧 (status フィルタ)
- 1件の承認（freee 取引登録）
- 消し込み再試行（reconcile）
- Gmail スキャンの起動（subprocess でバックグラウンド起動 → 即リダイレクト）

scan は数分かかるため、HTTP リクエスト内で完結させずサブプロセスで投げる。
ジョブ完了は画面リロードで確認する素朴な実装。
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from accounting.config import settings
from accounting.connectors.freee import FreeeClient
from accounting.core.db import init_db
from accounting.core.dry_run import DryRunContext
from accounting.core.idempotency import is_executed
from accounting.core.logger import bind_run, get_logger, unbind_run
from accounting.core.report import generate_run_id
from accounting.core.vendor_invoice_candidates import (
    get_by_id,
    list_by_status,
    update_status,
)
from accounting.tasks.vendor_invoice import (
    account_resolver,
    partner_matcher,
    reconciler,
    registrar,
)
from accounting.tasks.vendor_invoice.blacklists import get_known_vendor
from accounting.web.app import templates

router = APIRouter(prefix="/tasks/vendor-invoice")
log = get_logger("web.vendor_invoice")
TASK_NAME = "vendor_invoice"

REPO_ROOT = Path(__file__).resolve().parents[3]
JOBS_DIR = REPO_ROOT / "logs" / "web_jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

# 表示しない（つまり一覧で初期にチェックを外している）ステータス
ALL_STATUSES = [
    "pending",
    "registered",
    "reconciled",
    "unpaid",
    "manual_review",
    "excluded",
    "failed",
]
DEFAULT_STATUSES = ["pending", "registered", "unpaid", "manual_review", "failed"]


def _selected_statuses(query: str | None) -> list[str]:
    if not query:
        return DEFAULT_STATUSES
    vals = [v.strip() for v in query.split(",") if v.strip()]
    return [v for v in vals if v in ALL_STATUSES] or DEFAULT_STATUSES


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    init_db()
    statuses_param = request.query_params.get("status")
    selected = _selected_statuses(statuses_param)
    rows = list_by_status(selected)

    # 集計（全 status のカウント）
    counter: dict[str, int] = {s: 0 for s in ALL_STATUSES}
    for s in ALL_STATUSES:
        if s in selected:
            counter[s] = len(list_by_status([s]))
        else:
            counter[s] = len(list_by_status([s]))

    return templates.TemplateResponse(
        request,
        "tasks/vendor_invoice/list.html",
        {
            "rows": rows,
            "selected": selected,
            "all_statuses": ALL_STATUSES,
            "counter": counter,
            "dry_run": settings.dry_run,
        },
    )


@router.get("/{candidate_id}", response_class=HTMLResponse)
async def detail(request: Request, candidate_id: int) -> HTMLResponse:
    init_db()
    c = get_by_id(candidate_id)
    if c is None:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": f"id={candidate_id} の候補が見つかりません", "dry_run": settings.dry_run},
            status_code=404,
        )
    external_id = registrar.build_external_id(
        c.gmail_message_id, c.gmail_attachment_id or None
    )
    already_registered = is_executed(TASK_NAME, external_id)
    return templates.TemplateResponse(
        request,
        "tasks/vendor_invoice/detail.html",
        {
            "c": c,
            "external_id": external_id,
            "already_registered": already_registered,
            "dry_run": settings.dry_run,
        },
    )


@router.post("/{candidate_id}/apply", response_class=HTMLResponse)
async def apply(
    request: Request,
    candidate_id: int,
    dry_run: str | None = Form(default=None),
) -> HTMLResponse:
    init_db()
    is_dry = dry_run is not None
    c = get_by_id(candidate_id)
    if c is None:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "候補が見つかりません", "dry_run": is_dry},
            status_code=404,
        )
    if c.classification != "bank_transfer_invoice":
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "message": f"classification={c.classification} は登録対象外（excluded など）",
                "dry_run": is_dry,
            },
            status_code=400,
        )

    run_id = generate_run_id(TASK_NAME)
    bind_run(TASK_NAME, run_id)
    status_label = "failed"
    error: str | None = None
    deal_id: int | None = None
    try:
        with DryRunContext(is_dry):
            with FreeeClient() as freee:
                # partner / account_item が未引き当てなら再試行
                if c.freee_partner_id is None or c.freee_account_item_id is None:
                    pm = partner_matcher.match_partner(
                        freee,
                        c.extracted_partner_name,
                        fallback_hint=(get_known_vendor(c.sender) or {}).get(
                            "partner_name_hint"
                        ),
                    )
                    if pm.partner_id is None:
                        raise ValueError(
                            "partner が引き当てられません。freee 側で取引先を登録してください。"
                        )
                    hint = get_known_vendor(c.sender) or {}
                    res = account_resolver.resolve_account_item(
                        freee,
                        partner_id=pm.partner_id,
                        default_hint_name=hint.get("default_account_item"),
                    )
                    if res.account_item_id is None:
                        raise ValueError("account_item が引き当てられません。")
                    update_status(
                        c.id,
                        c.status,
                        freee_partner_id=pm.partner_id,
                        freee_account_item_id=res.account_item_id,
                        freee_account_item_name=res.account_item_name,
                    )
                    c = get_by_id(candidate_id) or c

                if c.extracted_amount is None:
                    raise ValueError("抽出金額が空です。手動で freee に登録してください。")

                payload = registrar.build_deal_payload(
                    company_id=int(settings.freee_company_id or 0),
                    partner_id=c.freee_partner_id,  # type: ignore[arg-type]
                    issue_date=c.extracted_issue_date or date.today(),
                    due_date=c.extracted_due_date,
                    total_amount=c.extracted_amount,
                    expense_account_item_id=c.freee_account_item_id,  # type: ignore[arg-type]
                    tax_code=account_resolver.get_unfiled_tax_code(freee),
                    description=(
                        f"{c.extracted_partner_name or c.sender} "
                        f"{c.extracted_summary or ''}"
                    ).strip(),
                )
                ext_id = registrar.build_external_id(
                    c.gmail_message_id, c.gmail_attachment_id or None
                )
                result = registrar.register_deal(freee, payload, ext_id, run_id)
                if result.get("dry_run"):
                    status_label = "dry_run"
                elif result.get("skipped"):
                    status_label = "skipped"
                else:
                    deal_id = result.get("deal_id")
                    update_status(c.id, "registered", freee_deal_id=deal_id)
                    # reconcile 試行
                    latest = get_by_id(c.id)
                    if latest is not None:
                        reconciler.reconcile_candidate(freee, latest)
                    refreshed = get_by_id(c.id)
                    status_label = (refreshed.status if refreshed else "registered")
    except Exception as e:
        log.exception("web.vendor_invoice.apply_failed", candidate_id=candidate_id)
        error = str(e)
        if not is_dry:
            update_status(c.id, "failed", error_message=f"web_apply: {e}")
    finally:
        unbind_run()

    return templates.TemplateResponse(
        request,
        "tasks/vendor_invoice/result.html",
        {
            "candidate_id": candidate_id,
            "status_label": status_label,
            "deal_id": deal_id,
            "error": error,
            "dry_run": is_dry,
        },
        status_code=200 if error is None else 500,
    )


@router.post("/reconcile", response_class=HTMLResponse)
async def reconcile_endpoint(
    request: Request,
    dry_run: str | None = Form(default=None),
) -> HTMLResponse:
    init_db()
    is_dry = dry_run is not None
    run_id = generate_run_id(TASK_NAME)
    bind_run(TASK_NAME, run_id)
    try:
        with DryRunContext(is_dry):
            results = reconciler.reconcile_pending()
    except Exception as e:
        log.exception("web.vendor_invoice.reconcile_failed")
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": f"reconcile 失敗: {e}", "dry_run": is_dry},
            status_code=500,
        )
    finally:
        unbind_run()

    matched = sum(1 for r in results if r.matched)
    return templates.TemplateResponse(
        request,
        "tasks/vendor_invoice/reconcile_result.html",
        {
            "results": results,
            "matched": matched,
            "total": len(results),
            "dry_run": is_dry,
        },
    )


@router.post("/scan", response_class=HTMLResponse)
async def scan_endpoint(
    request: Request,
    days: int = Form(30),
    dry_run: str | None = Form(default=None),
) -> Any:
    """`accounting vendor-invoice scan --days N [--no-dry-run]` をサブプロセスで起動。

    終了を待たず即リダイレクト。標準出力は logs/web_jobs/{run_id}.log に書く。
    """
    is_dry = dry_run is not None
    run_id = generate_run_id(TASK_NAME)
    out_path = JOBS_DIR / f"scan-{run_id}.log"

    cmd = [
        sys.executable,
        "-m",
        "accounting.cli",
        "vendor-invoice",
        "scan",
        "--days",
        str(int(days)),
    ]
    cmd.append("--dry-run" if is_dry else "--no-dry-run")
    cmd.append("--no-notify")  # Web UI 側で確認するので Resend 通知は抑制

    log.info("web.vendor_invoice.scan_started", run_id=run_id, cmd=cmd, dry_run=is_dry)
    with open(out_path, "w") as f:
        subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            stdout=f,
            stderr=subprocess.STDOUT,
            env={**os.environ},
            close_fds=True,
            start_new_session=True,
        )
    return RedirectResponse(url="/tasks/vendor-invoice?scan_started=1", status_code=303)
