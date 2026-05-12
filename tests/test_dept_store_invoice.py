from __future__ import annotations

from datetime import date

import pytest


@pytest.fixture
def seibu_env(monkeypatch):
    """テスト用の最低限の freee/vendor env を埋める。"""
    monkeypatch.setenv("VENDOR_MAP_SEIBU", "12345,株式会社そごう・西武")
    monkeypatch.setenv("FREEE_COMPANY_ID", "9999")
    monkeypatch.setenv("FREEE_ACCOUNT_ITEM_SALES", "100")
    monkeypatch.setenv("FREEE_ACCOUNT_ITEM_RECEIVABLE", "200")
    monkeypatch.setenv("FREEE_ACCOUNT_ITEM_COMMISSION", "300")
    monkeypatch.setenv("FREEE_TAX_CODE_SALES", "21")
    monkeypatch.setenv("FREEE_TAX_CODE_FEE", "0")


def _make_stmt(gross: int, transfer: int):
    from accounting.tasks.dept_store_invoice import DeptStoreStatement

    return DeptStoreStatement(
        vendor_name="株式会社そごう・西武",
        vendor_registration_number="T1234567890123",
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
        issued_date=date(2026, 4, 15),
        payment_date=date(2026, 4, 30),
        gross_sales=gross,
        purchase_total=None,
        transfer_amount=transfer,
    )


def test_build_journal_payload_balances(seibu_env):
    from accounting.tasks.dept_store_invoice import _build_journal_payload

    stmt = _make_stmt(gross=2_167_212, transfer=1_710_064)
    payload = _build_journal_payload(stmt, "seibu")

    debits = sum(d["amount"] for d in payload["details"] if d["entry_side"] == "debit")
    credits = sum(d["amount"] for d in payload["details"] if d["entry_side"] == "credit")
    assert debits == credits == 2_167_212

    sides = {d["entry_side"]: d["amount"] for d in payload["details"]}
    assert sides["credit"] == 2_167_212  # 売上高

    debit_amounts = sorted(
        d["amount"] for d in payload["details"] if d["entry_side"] == "debit"
    )
    assert debit_amounts == [457_148, 1_710_064]  # 支払手数料, 売掛金

    # partner_id, issue_date, description が想定通り
    assert all(d["partner_id"] == "12345" for d in payload["details"])
    assert payload["issue_date"] == "2026-03-31"
    assert "2026-03-01〜2026-03-31" in payload["description"]


def test_build_journal_payload_negative_fee_raises(seibu_env):
    from accounting.tasks.dept_store_invoice import _build_journal_payload

    stmt = _make_stmt(gross=1_000_000, transfer=1_500_000)
    with pytest.raises(ValueError, match="差額が負"):
        _build_journal_payload(stmt, "seibu")


def test_external_id_format():
    from accounting.tasks.dept_store_invoice import _build_external_id

    assert _build_external_id("seibu", date(2026, 3, 31)) == "dept-store-seibu-20260331"
    assert _build_external_id("marui", date(2026, 12, 1)) == "dept-store-marui-20261201"


def test_vendor_partner_id_placeholder_raises(monkeypatch):
    """プレースホルダ値（__SEIBU_PARTNER_ID__）のままだと早期にエラーになる。"""
    from accounting.config import settings

    monkeypatch.setenv("VENDOR_MAP_SEIBU", "__SEIBU_PARTNER_ID__,株式会社そごう・西武")
    with pytest.raises(ValueError, match="プレースホルダ"):
        settings.vendor_partner_id("seibu")


def test_vendor_partner_id_unset_raises(monkeypatch):
    from accounting.config import settings

    monkeypatch.delenv("VENDOR_MAP_NONEXISTENT", raising=False)
    with pytest.raises(ValueError, match="未設定"):
        settings.vendor_partner_id("nonexistent")


def test_vision_extractor_is_mocked_not_called(mocker, seibu_env, tmp_path, monkeypatch):
    """run() の中で VisionExtractor.extract が mock で差し替えられ、実APIは呼ばれない。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    # init_db のため一時 SQLite を使う
    from accounting.config import settings as cfg
    from accounting.core import db as db_module

    original_path = cfg.database_path
    cfg.database_path = str(tmp_path / "test.db")
    db_module._engine = None
    db_module._SessionLocal = None

    try:
        from accounting.tasks import dept_store_invoice as task

        stmt = _make_stmt(gross=2_167_212, transfer=1_710_064)
        mock_extract = mocker.patch.object(
            task.VisionExtractor, "extract", return_value=stmt
        )
        mocker.patch.object(task.VisionExtractor, "__init__", return_value=None)

        # dry-run コンテキストで実行（freeeにも触らない）
        from accounting.core.dry_run import DryRunContext

        with DryRunContext(True):
            report = task.run([tmp_path / "dummy.jpg"], "seibu")

        assert mock_extract.called
        assert report.failure_count == 0
        assert report.success_count == 1
    finally:
        cfg.database_path = original_path
        db_module._engine = None
        db_module._SessionLocal = None
