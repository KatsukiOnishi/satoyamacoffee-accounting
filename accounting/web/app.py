"""FastAPI app factory。テストからもサーバからも同じ create_app() を使う。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from accounting.core.db import init_db
from accounting.core.extractions import cleanup_old
from accounting.web.auth import TokenAuthMiddleware

WEB_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def create_app(auth_token: str) -> FastAPI:
    init_db()
    cleanup_old(hours=24)

    app = FastAPI(title="さとやまコーヒー 月次決算ハブ", docs_url=None, redoc_url=None)
    app.state.auth_token = auth_token
    app.state.templates = templates

    # 認証ミドルウェア
    app.add_middleware(TokenAuthMiddleware)

    # 静的ファイル（存在しない場合もマウントできる）
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ルーティング
    from accounting.web.routes import (
        dashboard,
        dept_store_invoice,
        inventory_valuation,
        payroll,
        vendor_invoice,
    )

    app.include_router(dashboard.router)
    app.include_router(dept_store_invoice.router)
    app.include_router(vendor_invoice.router)
    app.include_router(inventory_valuation.router)
    app.include_router(payroll.router)

    return app
