from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from accounting.config import settings
from accounting.web.app import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    tasks = [
        {
            "title": "百貨店明細取込",
            "description": "買掛金支払明細書の写真から売上仕訳を起こして freee に登録する。",
            "href": "/tasks/dept-store-invoice/upload",
            "badge": None,  # 将来「今月実行済み」等を入れる
        },
    ]
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"tasks": tasks, "dry_run": settings.dry_run},
    )
