from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from accounting.config import settings
from accounting.core import extractions
from accounting.core.dry_run import DryRunContext
from accounting.core.logger import bind_run, get_logger, unbind_run
from accounting.core.report import generate_run_id
from accounting.tasks.dept_store_invoice import (
    DeptStoreStatement,
    build_journal_with_validation,
    extract_statement,
    register_to_freee,
)
from accounting.web.app import templates

router = APIRouter(prefix="/tasks/dept-store-invoice")

SAMPLES_DIR = Path(__file__).resolve().parents[3] / "samples"

log = get_logger("web.dept_store_invoice")


def _save_uploads(files: list[UploadFile], vendor: str, period_yyyymm: str) -> list[Path]:
    target_dir = SAMPLES_DIR / f"{vendor}-{period_yyyymm}"
    target_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    saved: list[Path] = []
    for i, uf in enumerate(files):
        # ファイル名衝突を避けるためタイムスタンプ + index を付与
        original = Path(uf.filename or f"upload-{i}.bin").name
        stem, suffix = Path(original).stem, Path(original).suffix
        dest = target_dir / f"{ts}-{i:02d}-{stem}{suffix or '.bin'}"
        with dest.open("wb") as f:
            shutil.copyfileobj(uf.file, f)
        saved.append(dest)
    return saved


@router.get("/upload", response_class=HTMLResponse)
async def upload_form(request: Request) -> HTMLResponse:
    vendors = settings.list_vendors()
    return templates.TemplateResponse(
        request,
        "tasks/dept_store_invoice/upload.html",
        {"vendors": vendors, "dry_run": settings.dry_run},
    )


@router.post("/extract", response_class=HTMLResponse)
async def extract_endpoint(
    request: Request,
    vendor: str = Form(...),
    period_yyyymm: str = Form(...),
    files: list[UploadFile] = File(...),
    dry_run: str | None = Form(default=None),
) -> HTMLResponse:
    is_dry = dry_run is not None

    if not files or all(not f.filename for f in files):
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "ファイルが選択されていません。", "dry_run": is_dry},
            status_code=400,
        )

    saved_paths = _save_uploads(files, vendor, period_yyyymm)
    log.info("web.extract.uploaded", vendor=vendor, period=period_yyyymm, count=len(saved_paths))

    try:
        stmt: DeptStoreStatement = extract_statement(saved_paths)
    except Exception as e:
        log.exception("web.extract.failed", error=str(e))
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": f"抽出に失敗しました: {e}", "dry_run": is_dry},
            status_code=500,
        )

    try:
        payload, external_id, warnings = build_journal_with_validation(stmt, vendor)
    except ValueError as e:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": f"妥当性チェックに失敗: {e}", "dry_run": is_dry},
            status_code=400,
        )

    extraction_id = extractions.create(
        task="dept_store_invoice",
        vendor_slug=vendor,
        period_yyyymm=period_yyyymm,
        statement_json=stmt.model_dump_json(),
        image_paths=[str(p) for p in saved_paths],
    )

    return templates.TemplateResponse(
        request,
        "tasks/dept_store_invoice/preview.html",
        {
            "stmt": stmt,
            "payload": payload,
            "external_id": external_id,
            "warnings": warnings,
            "extraction_id": extraction_id,
            "dry_run": is_dry,
        },
    )


@router.post("/register", response_class=HTMLResponse)
async def register_endpoint(
    request: Request,
    extraction_id: str = Form(...),
    dry_run: str | None = Form(default=None),
) -> HTMLResponse:
    is_dry = dry_run is not None

    record = extractions.get(extraction_id)
    if record is None:
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "message": "抽出データが見つかりません（期限切れの可能性）。最初からやり直してください。",
                "dry_run": is_dry,
            },
            status_code=404,
        )

    stmt = DeptStoreStatement.model_validate_json(record["statement_json"])
    vendor = record["vendor_slug"]

    try:
        payload, external_id, _ = build_journal_with_validation(stmt, vendor)
    except ValueError as e:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": f"妥当性チェックに失敗: {e}", "dry_run": is_dry},
            status_code=400,
        )

    run_id = generate_run_id("dept-store-invoice")
    bind_run("dept_store_invoice", run_id)
    try:
        with DryRunContext(is_dry):
            result = register_to_freee(payload, external_id, run_id)
    except Exception as e:
        log.exception("web.register.failed", error=str(e))
        return templates.TemplateResponse(
            request,
            "tasks/dept_store_invoice/result.html",
            {
                "status": "failed",
                "external_id": external_id,
                "error": str(e),
                "dry_run": is_dry,
            },
            status_code=500,
        )
    finally:
        unbind_run()

    if result.get("skipped"):
        status = "skipped"
        journal_id = None
    elif result.get("dry_run"):
        status = "dry_run"
        journal_id = None
    else:
        status = "success"
        journal_id = result.get("journal_id")
        # 登録成功したら pending_extraction は削除
        extractions.delete_one(extraction_id)

    return templates.TemplateResponse(
        request,
        "tasks/dept_store_invoice/result.html",
        {
            "status": status,
            "external_id": external_id,
            "journal_id": journal_id,
            "error": None,
            "dry_run": is_dry,
        },
    )
