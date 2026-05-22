"""email-digest タスクの単体テスト。"""
from __future__ import annotations

from datetime import date, datetime

import pytest


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    from accounting.config import settings
    from accounting.core import db as db_module

    monkeypatch.setattr(settings, "database_path", str(tmp_path / "test_ed.db"))
    db_module._engine = None
    db_module._SessionLocal = None
    db_module.init_db()
    yield tmp_path
    db_module._engine = None
    db_module._SessionLocal = None


def test_week_range_for_monday_start():
    from accounting.tasks.email_digest.aggregator import week_range_for

    # 2026-05-22 は金曜日 → 月曜 = 5/18, 日曜 = 5/24
    start, end = week_range_for(date(2026, 5, 22))
    assert start == date(2026, 5, 18)
    assert end == date(2026, 5, 24)


def test_iso_week_label():
    from accounting.tasks.email_digest.aggregator import iso_week_label

    assert iso_week_label(date(2026, 5, 18)) == "2026-W21"


def test_week_range_from_iso():
    from accounting.tasks.email_digest.aggregator import week_range_from_iso

    start, end = week_range_from_iso("2026-W21")
    assert start == date(2026, 5, 18)
    assert end == date(2026, 5, 24)


def test_aggregate_empty(isolated_db):
    from accounting.tasks.email_digest.aggregator import aggregate

    digest = aggregate(date(2026, 5, 18), date(2026, 5, 24))
    assert digest.iso_week == "2026-W21"
    assert digest.mode == "shadow"
    assert digest.ar_reconciled == []
    assert digest.total_success == 0


def test_aggregate_with_data(isolated_db):
    from accounting.core import auto_keiri
    from accounting.tasks.email_digest.aggregator import aggregate

    # ar-reconcile 候補を 2 件挿入（reconciled / unmatched）
    auto_keiri.insert_ar_candidate(
        run_id="r1",
        run_started_at=datetime.utcnow(),
        wallet_txn_id=1,
        wallet_txn_date=date(2026, 5, 20),
        wallet_txn_description="振込 カ）イネトアガベ",
        wallet_txn_amount=107380,
        matched_invoice_id=42,
        matched_partner_name="稲とアガベ株式会社",
        matched_invoice_amount=107380,
        matched_invoice_issue_date=date(2026, 3, 31),
        status="reconciled",
    )
    auto_keiri.insert_ar_candidate(
        run_id="r1",
        run_started_at=datetime.utcnow(),
        wallet_txn_id=2,
        wallet_txn_date=date(2026, 5, 21),
        wallet_txn_description="振込 イシカワ カナコ",
        wallet_txn_amount=141960,
        status="unmatched",
    )
    # auto-classify を 2 件挿入（shadow_logged, review_required）
    auto_keiri.insert_classify_candidate(
        run_id="rc1",
        run_started_at=datetime.utcnow(),
        mode="shadow",
        wallet_txn_id=3,
        wallet_txn_date=date(2026, 5, 22),
        wallet_txn_description="YAMATO",
        wallet_txn_amount=-9535,
        classified_account_item_name="荷造運賃",
        classified_tax_code_name="課対仕入10%",
        classification_confidence=0.92,
        classification_reason="YAMATO は配送料",
        action_taken="shadow_logged",
    )
    auto_keiri.insert_classify_candidate(
        run_id="rc1",
        run_started_at=datetime.utcnow(),
        mode="shadow",
        wallet_txn_id=4,
        wallet_txn_date=date(2026, 5, 22),
        wallet_txn_description="DAISO",
        wallet_txn_amount=-330,
        classified_account_item_name="消耗品費",
        classified_tax_code_name="課対仕入10%",
        classification_confidence=0.55,
        classification_reason="個人立替の可能性",
        action_taken="shadow_logged",
    )

    digest = aggregate(date(2026, 5, 18), date(2026, 5, 24))
    assert len(digest.ar_reconciled) == 1
    assert digest.ar_reconciled[0].partner_name == "稲とアガベ株式会社"
    assert len(digest.ar_unmatched) == 1
    assert len(digest.classify_shadow_logged) == 2


def test_compose_render_subject_shadow():
    from accounting.tasks.email_digest.composer import render_subject
    from accounting.tasks.email_digest.models import WeeklyDigest

    digest = WeeklyDigest(
        week_start=date(2026, 5, 18),
        week_end=date(2026, 5, 24),
        iso_week="2026-W21",
        mode="shadow",
    )
    subj = render_subject(digest)
    assert "[さとやま経理]" in subj
    assert "2026-W21" in subj
    assert "5/18-5/24" in subj
    assert "成功0件" in subj


def test_compose_render_html_shadow(isolated_db):
    from accounting.tasks.email_digest.aggregator import aggregate
    from accounting.tasks.email_digest.composer import render_html

    digest = aggregate(date(2026, 5, 18), date(2026, 5, 24), mode="shadow")
    html = render_html(digest)
    assert "<html" in html
    assert "auto-keiri 週次ダイジェスト" in html
    # shadow バッジ付与
    assert "shadow" in html


def test_compose_render_html_with_data_shadow(isolated_db):
    from accounting.core import auto_keiri
    from accounting.tasks.email_digest.aggregator import aggregate
    from accounting.tasks.email_digest.composer import render_html

    auto_keiri.insert_ar_candidate(
        run_id="r1",
        run_started_at=datetime.utcnow(),
        wallet_txn_id=1,
        wallet_txn_date=date(2026, 5, 20),
        wallet_txn_description="振込 イネトアガベ",
        wallet_txn_amount=107380,
        matched_invoice_id=42,
        matched_partner_name="稲とアガベ株式会社",
        matched_invoice_amount=107380,
        status="reconciled",
    )
    digest = aggregate(date(2026, 5, 18), date(2026, 5, 24), mode="shadow")
    html = render_html(digest)
    assert "稲とアガベ株式会社" in html
    assert "107,380" in html
    # シャドー検証手順
    assert "set-mode --mode production" in html


def test_body_summary_shadow(isolated_db):
    from accounting.tasks.email_digest.aggregator import aggregate
    from accounting.tasks.email_digest.composer import body_summary

    digest = aggregate(date(2026, 5, 18), date(2026, 5, 24), mode="shadow")
    s = body_summary(digest)
    assert "shadow_logged" in s


def test_send_dry_run_returns_success():
    """dry_run=True なら Resend を叩かず success 扱い。"""
    from accounting.tasks.email_digest.sender import send_digest

    res = send_digest(subject="test", html="<p>hi</p>", dry_run=True)
    assert res.success is True
    assert res.resend_message_id is None
