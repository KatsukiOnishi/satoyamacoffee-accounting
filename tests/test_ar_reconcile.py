"""ar-reconcile タスクの単体テスト。"""
from __future__ import annotations

from datetime import date

import pytest


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """テストごとに独立した SQLite を使う。"""
    from accounting.config import settings
    from accounting.core import db as db_module

    monkeypatch.setattr(settings, "database_path", str(tmp_path / "test_ar.db"))
    db_module._engine = None
    db_module._SessionLocal = None
    db_module.init_db()
    yield tmp_path
    db_module._engine = None
    db_module._SessionLocal = None


# ---- excluder ----


def test_excluder_personal_kana_basic():
    from accounting.tasks.ar_reconcile.excluder import is_personal_kana_transfer

    assert is_personal_kana_transfer("振込 カネコ マリ") is True
    assert is_personal_kana_transfer("振込 ミナト チホ") is True
    assert is_personal_kana_transfer("振込 ヨシオカ リナ") is True


def test_excluder_personal_kana_company_excluded():
    from accounting.tasks.ar_reconcile.excluder import is_personal_kana_transfer

    # 法人略称を含む振込は個人扱いしない
    assert is_personal_kana_transfer("振込 カ）イネトアガベ") is False
    assert is_personal_kana_transfer("振込 （株）アウトクロップ") is False


def test_excluder_dept_store():
    from accounting.tasks.ar_reconcile.excluder import is_dept_store_transfer

    assert is_dept_store_transfer("振込 カ）ソゴウ．セイブ") is True
    assert is_dept_store_transfer("そごう・西武 売上") is True
    assert is_dept_store_transfer("ヤマトトランスポート") is False


def test_excluder_jfc_loan():
    from accounting.tasks.ar_reconcile.excluder import is_jfc_loan

    assert is_jfc_loan("日本公庫 返済") is True
    assert is_jfc_loan("公庫 利息") is True
    assert is_jfc_loan("公的金融機関") is False


def test_excluder_bank_fee():
    from accounting.tasks.ar_reconcile.excluder import is_bank_fee

    assert is_bank_fee("振込手数料", 220) is True
    assert is_bank_fee("振込手数料", 110) is True
    assert is_bank_fee("振込手数料", 500) is False  # 金額帯外
    assert is_bank_fee("ヤマト運輸", 220) is False  # 摘要不一致


def test_excluder_interest():
    from accounting.tasks.ar_reconcile.excluder import is_interest

    assert is_interest("決算お利息") is True
    assert is_interest("受取利息") is True
    assert is_interest("貸付利息") is False


def test_ar_reconcile_exclusion_reason():
    from accounting.tasks.ar_reconcile.excluder import ar_reconcile_exclusion_reason

    assert ar_reconcile_exclusion_reason("振込 カネコ マリ") == "personal_kana"
    assert ar_reconcile_exclusion_reason("振込 カ）ソゴウ．セイブ") == "dept_store"
    assert ar_reconcile_exclusion_reason("受取利息") == "interest"
    assert ar_reconcile_exclusion_reason("ヤマト運輸 株式会社") is None


def test_auto_classify_exclusion_reason():
    from accounting.tasks.ar_reconcile.excluder import auto_classify_exclusion_reason

    assert auto_classify_exclusion_reason("振込 ミナト チホ", -200000) == "personal_kana"
    assert auto_classify_exclusion_reason("そごう振込", 100000) == "dept_store"
    assert auto_classify_exclusion_reason("日本公庫 引落", -50000) == "jfc_loan"
    assert auto_classify_exclusion_reason("振込手数料", -220) == "bank_fee"
    assert auto_classify_exclusion_reason("ヤマト運輸", -1000) is None


# ---- matcher ----


def _txn(amount: int, desc: str = "", *, txn_id: int = 1):
    from accounting.tasks.ar_reconcile.models import WalletTxnIncome

    return WalletTxnIncome(
        id=txn_id,
        date=date(2026, 5, 1),
        description=desc,
        amount=amount,
        walletable_type="bank_account",
        walletable_id=100,
        walletable_name="普通預金",
        entry_side="income",
    )


def _invoice(amount: int, partner: str | None, *, deal_id: int = 1):
    from accounting.tasks.ar_reconcile.models import UnsettledInvoice

    return UnsettledInvoice(
        deal_id=deal_id,
        partner_id=10,
        partner_name=partner,
        total_amount=amount,
        issue_date=date(2026, 4, 25),
    )


def test_matcher_single_amount_match():
    from accounting.tasks.ar_reconcile.matcher import match_txn

    txn = _txn(107380, "振込 カ）イネトアガベ")
    inv = _invoice(107380, "稲とアガベ株式会社", deal_id=42)
    candidate = match_txn(txn, [inv])
    assert candidate.status == "matched"
    assert len(candidate.candidates) == 1
    assert candidate.candidates[0].deal_id == 42


def test_matcher_unmatched():
    from accounting.tasks.ar_reconcile.matcher import match_txn

    txn = _txn(99999, "振込 ABC")
    inv = _invoice(10000, "他社")
    candidate = match_txn(txn, [inv])
    assert candidate.status == "unmatched"
    assert candidate.candidates == []


def test_matcher_multiple_amount_match():
    from accounting.tasks.ar_reconcile.matcher import match_txn

    txn = _txn(10000, "振込 不明取引先")
    inv1 = _invoice(10000, "甲社")
    inv2 = _invoice(10000, "乙社")
    candidate = match_txn(txn, [inv1, inv2])
    assert candidate.status == "multiple_matches"
    assert len(candidate.candidates) == 2


def test_matcher_partner_disambiguates_multiple_amount():
    """同金額が複数あっても、partner 名が一致するもの 1 件に絞り込める。"""
    from accounting.tasks.ar_reconcile.matcher import match_txn

    txn = _txn(10929, "振込 アウトクロップ")
    inv1 = _invoice(10929, "アウトクロップ株式会社", deal_id=100)
    inv2 = _invoice(10929, "別の取引先", deal_id=101)
    candidate = match_txn(txn, [inv1, inv2])
    assert candidate.status == "matched"
    assert candidate.candidates[0].deal_id == 100


def test_normalize_partner_name():
    from accounting.tasks.ar_reconcile.matcher import normalize_partner_name

    # 法人略称・空白・記号を除去
    assert normalize_partner_name("株式会社さとやま") == "さとやま"
    assert normalize_partner_name("(株)さとやま") == "さとやま"
    assert normalize_partner_name("合同会社 秋田 里山 デザイン") == "秋田里山デザイン"
    assert normalize_partner_name(None) == ""


# ---- reconciler (DB 永続化部分) ----


def test_serialize_for_db_with_match(isolated_db):
    from accounting.tasks.ar_reconcile.matcher import match_txn
    from accounting.tasks.ar_reconcile.reconciler import serialize_for_db

    txn = _txn(107380, "振込 カ）イネトアガベ")
    inv = _invoice(107380, "稲とアガベ株式会社", deal_id=42)
    candidate = match_txn(txn, [inv])
    candidate.status = "reconciled"
    row = serialize_for_db(candidate, run_id="run-1")
    assert row["wallet_txn_id"] == 1
    assert row["wallet_txn_amount"] == 107380
    assert row["matched_invoice_id"] == 42
    assert row["matched_partner_name"] == "稲とアガベ株式会社"
    assert row["status"] == "reconciled"


def test_insert_ar_candidate_idempotent(isolated_db):
    """同じ wallet_txn_id を 2 回 insert しても 1 行だけになる。"""
    from datetime import datetime

    from accounting.core import auto_keiri

    base = dict(
        run_id="run-1",
        run_started_at=datetime.utcnow(),
        wallet_txn_id=12345,
        wallet_txn_date=date(2026, 5, 1),
        wallet_txn_description="振込 カ）イネトアガベ",
        wallet_txn_amount=107380,
        status="unmatched",
    )
    auto_keiri.insert_ar_candidate(**base)
    # 同じ wallet_txn を別の状態で上書き
    base["status"] = "reconciled"
    base["matched_invoice_id"] = 42
    auto_keiri.insert_ar_candidate(**base)

    rows = auto_keiri.list_ar_candidates_in_range(date(2026, 5, 1), date(2026, 5, 2))
    assert len(rows) == 1
    assert rows[0].status == "reconciled"
    assert rows[0].matched_invoice_id == 42
