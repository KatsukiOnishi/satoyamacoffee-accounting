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
            "title": "ベンダー請求書取込",
            "description": "Gmail からベンダー請求書を取込み、freee に「Dr.費用 / Cr.未払金」で登録 → 振込時に消し込み。",
            "href": "/tasks/vendor-invoice",
            "badge": None,
        },
        {
            "title": "月次在庫評価仕訳",
            "description": "coffee_system の当月末評価額を取得し、商品 / 期末商品棚卸高 の振替伝票を登録（前月逆仕訳付き）。",
            "href": "/tasks/inventory-valuation",
            "badge": None,
        },
        {
            "title": "百貨店明細取込",
            "description": "買掛金支払明細書の写真から売上仕訳を起こして freee に登録する。",
            "href": "/tasks/dept-store-invoice/upload",
            "badge": None,
        },
    ]
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"tasks": tasks, "dry_run": settings.dry_run},
    )
