from __future__ import annotations

import io
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-token-xyz"


@pytest.fixture
def web_env(tmp_path, monkeypatch):
    """テスト用に DB と samples ディレクトリを一時パスへ差し替える。"""
    monkeypatch.setenv("VENDOR_MAP_SEIBU", "12345,株式会社そごう・西武")
    monkeypatch.setenv("FREEE_COMPANY_ID", "9999")
    monkeypatch.setenv("FREEE_ACCOUNT_ITEM_SALES", "100")
    monkeypatch.setenv("FREEE_ACCOUNT_ITEM_RECEIVABLE", "200")
    monkeypatch.setenv("FREEE_ACCOUNT_ITEM_COMMISSION", "300")
    monkeypatch.setenv("FREEE_TAX_CODE_SALES", "21")
    monkeypatch.setenv("FREEE_TAX_CODE_FEE", "0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")

    from accounting.config import settings as cfg
    from accounting.core import db as db_module

    original_db = cfg.database_path
    cfg.database_path = str(tmp_path / "test_web.db")
    db_module._engine = None
    db_module._SessionLocal = None

    # samples 保存先を tmp_path に向ける
    from accounting.web.routes import dept_store_invoice as route

    original_samples = route.SAMPLES_DIR
    route.SAMPLES_DIR = tmp_path / "samples"

    yield tmp_path

    cfg.database_path = original_db
    db_module._engine = None
    db_module._SessionLocal = None
    route.SAMPLES_DIR = original_samples


@pytest.fixture
def client(web_env):
    from accounting.web.app import create_app

    app = create_app(auth_token=TOKEN)
    return TestClient(app)


def test_unauthorized_returns_401(client):
    res = client.get("/")
    assert res.status_code == 401
    assert "認証" in res.text


def test_with_token_query_sets_cookie(client):
    # クエリ token 付きで初回アクセス → 200 + Cookie 設定
    res = client.get(f"/?token={TOKEN}")
    assert res.status_code == 200
    assert "accounting_token" in res.cookies
    assert res.cookies["accounting_token"] == TOKEN

    # 2回目はクエリなしでも Cookie で通る
    res2 = client.get("/")
    assert res2.status_code == 200


def test_dashboard_renders(client):
    res = client.get(f"/?token={TOKEN}")
    assert res.status_code == 200
    assert "百貨店明細取込" in res.text
    assert "/tasks/dept-store-invoice/upload" in res.text


def test_upload_form_renders_vendors(client):
    res = client.get(f"/tasks/dept-store-invoice/upload?token={TOKEN}")
    assert res.status_code == 200
    assert "株式会社そごう・西武" in res.text
    assert 'name="period_yyyymm"' in res.text


def _make_stmt():
    from accounting.tasks.dept_store_invoice import DeptStoreStatement

    return DeptStoreStatement(
        vendor_name="株式会社そごう・西武",
        vendor_registration_number="T1234567890123",
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
        issued_date=date(2026, 4, 15),
        payment_date=date(2026, 4, 30),
        gross_sales=2_167_212,
        purchase_total=1_803_124,
        transfer_amount=1_710_064,
    )


def test_upload_endpoint_saves_files_and_renders_preview(client, mocker, web_env):
    """multipart アップロード → 画像保存 → Vision モック → preview レンダリング。"""
    from accounting.web.routes import dept_store_invoice as route

    # ルート側でローカル束縛されている extract_statement を mock する
    mocker.patch.object(route, "extract_statement", return_value=_make_stmt())

    # 認証クッキーを先に取得
    client.get(f"/?token={TOKEN}")

    fake_image = io.BytesIO(b"\x89PNG\r\n\x1a\nfake")
    res = client.post(
        "/tasks/dept-store-invoice/extract",
        data={"vendor": "seibu", "period_yyyymm": "2026-03", "dry_run": "true"},
        files={"files": ("page1.png", fake_image, "image/png")},
    )
    assert res.status_code == 200, res.text
    assert "抽出結果のプレビュー" in res.text
    assert "dept-store-seibu-20260331" in res.text  # external_id

    # samples/{vendor}-{period}/ にファイルが保存されている
    saved_dir = web_env / "samples" / "seibu-2026-03"
    assert saved_dir.exists()
    files = list(saved_dir.iterdir())
    assert len(files) == 1
    assert files[0].suffix == ".png"


def test_register_endpoint_dry_run_does_not_mark_executed(client, mocker, web_env):
    """dry-run で register エンドポイントを叩いても is_executed は False のまま。"""
    from accounting.core import idempotency
    from accounting.web.routes import dept_store_invoice as route

    mocker.patch.object(route, "extract_statement", return_value=_make_stmt())

    # 認証クッキーを設定
    client.get(f"/?token={TOKEN}")

    # 1. 抽出して extraction_id を得る
    fake_image = io.BytesIO(b"\x89PNG\r\n\x1a\nfake")
    res = client.post(
        "/tasks/dept-store-invoice/extract",
        data={"vendor": "seibu", "period_yyyymm": "2026-03"},
        files={"files": ("page1.png", fake_image, "image/png")},
    )
    assert res.status_code == 200
    # HTML から extraction_id を抽出（hidden input）
    import re

    m = re.search(r'name="extraction_id" value="([0-9a-f]+)"', res.text)
    assert m, "extraction_id が preview ページに含まれていない"
    extraction_id = m.group(1)

    # 2. dry-run で register
    res2 = client.post(
        "/tasks/dept-store-invoice/register",
        data={"extraction_id": extraction_id, "dry_run": "true"},
    )
    assert res2.status_code == 200
    assert "dry-run のためスキップしました" in res2.text

    # 3. 冪等性テーブルには登録されていない
    assert idempotency.is_executed("dept_store_invoice", "dept-store-seibu-20260331") is False
