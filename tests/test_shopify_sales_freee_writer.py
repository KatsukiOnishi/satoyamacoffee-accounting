from __future__ import annotations

from datetime import date

import pytest

from accounting.tasks.shopify_sales.freee_writer import (
    AccountIds,
    build_external_id,
    build_manual_journal_payload,
    resolve_account_ids,
)
from accounting.tasks.shopify_sales.models import MonthlySummary, PartnerSummary


def _summary_2026_04():
    s = MonthlySummary(
        year=2026,
        month=4,
        period_start_jst=date(2026, 4, 1),
        period_end_jst=date(2026, 4, 30),
        order_count=47,
    )
    s.by_partner[900001] = PartnerSummary(
        partner_id=900001,
        partner_name="Shopify Payments",
        order_count=46,
        gross=137764,
        fee=4833,
    )
    s.by_partner[102026938] = PartnerSummary(
        partner_id=102026938,
        partner_name="株式会社デジカ（KOMOJU）",
        order_count=1,
        gross=4185,
        fee=151,
    )
    return s


def test_external_id_format():
    assert build_external_id(2026, 4) == "shopify-sales:2026-04"
    assert build_external_id(2026, 11) == "shopify-sales:2026-11"


def test_payload_debit_equals_credit():
    s = _summary_2026_04()
    payload = build_manual_journal_payload(
        summary=s,
        company_id=3206591,
        account_ids=AccountIds(receivable=512405349, sales=512405441, commission=512405479),
    )

    debits = sum(d["amount"] for d in payload["details"] if d["entry_side"] == "debit")
    credits = sum(d["amount"] for d in payload["details"] if d["entry_side"] == "credit")
    assert debits == credits
    assert credits == 141949  # 売上高合計 = total_gross


def test_payload_details_shape():
    s = _summary_2026_04()
    payload = build_manual_journal_payload(
        summary=s,
        company_id=3206591,
        account_ids=AccountIds(receivable=10, sales=20, commission=30),
    )
    assert payload["company_id"] == 3206591
    assert payload["issue_date"] == "2026-04-30"

    details = payload["details"]
    # 売上1行 + 売掛金2行 + 手数料2行 = 5行
    assert len(details) == 5

    credits = [d for d in details if d["entry_side"] == "credit"]
    assert len(credits) == 1
    assert credits[0]["account_item_id"] == 20
    assert credits[0]["amount"] == 141949

    receivables = [
        d for d in details if d["account_item_id"] == 10 and d["entry_side"] == "debit"
    ]
    assert len(receivables) == 2
    assert {r["partner_id"] for r in receivables} == {900001, 102026938}
    # net = gross - fee
    nets = {r["partner_id"]: r["amount"] for r in receivables}
    assert nets[900001] == 132931
    assert nets[102026938] == 4034

    commissions = [
        d for d in details if d["account_item_id"] == 30 and d["entry_side"] == "debit"
    ]
    assert len(commissions) == 2


def test_payload_omits_zero_fee_rows():
    s = MonthlySummary(
        year=2026,
        month=4,
        period_start_jst=date(2026, 4, 1),
        period_end_jst=date(2026, 4, 30),
        order_count=1,
    )
    s.by_partner[1] = PartnerSummary(
        partner_id=1, partner_name="No Fee", order_count=1, gross=1000, fee=0
    )
    payload = build_manual_journal_payload(
        summary=s,
        company_id=1,
        account_ids=AccountIds(receivable=10, sales=20, commission=30),
    )
    # 売上1 + 売掛金1（手数料0なので手数料行は無い）= 2行
    assert len(payload["details"]) == 2


def test_payload_zero_orders_raises():
    s = MonthlySummary(
        year=2026,
        month=4,
        period_start_jst=date(2026, 4, 1),
        period_end_jst=date(2026, 4, 30),
        order_count=0,
    )
    with pytest.raises(ValueError):
        build_manual_journal_payload(
            summary=s,
            company_id=1,
            account_ids=AccountIds(receivable=10, sales=20, commission=30),
        )


def test_resolve_account_ids_from_env(monkeypatch):
    monkeypatch.setenv("SHOPIFY_SALES_ACCOUNT_RECEIVABLE_ID", "100")
    monkeypatch.setenv("SHOPIFY_SALES_ACCOUNT_SALES_ID", "200")
    monkeypatch.setenv("SHOPIFY_SALES_ACCOUNT_COMMISSION_ID", "300")
    ids = resolve_account_ids()
    assert ids == AccountIds(receivable=100, sales=200, commission=300)


def test_resolve_account_ids_from_freee_items(monkeypatch):
    monkeypatch.delenv("SHOPIFY_SALES_ACCOUNT_RECEIVABLE_ID", raising=False)
    monkeypatch.delenv("SHOPIFY_SALES_ACCOUNT_SALES_ID", raising=False)
    monkeypatch.delenv("SHOPIFY_SALES_ACCOUNT_COMMISSION_ID", raising=False)
    items = [
        {"id": 1, "name": "売掛金"},
        {"id": 2, "name": "売上高"},
        {"id": 3, "name": "支払手数料"},
        {"id": 99, "name": "雑費"},
    ]
    ids = resolve_account_ids(account_items=items)
    assert ids == AccountIds(receivable=1, sales=2, commission=3)


def test_resolve_account_ids_missing_raises(monkeypatch):
    monkeypatch.delenv("SHOPIFY_SALES_ACCOUNT_RECEIVABLE_ID", raising=False)
    monkeypatch.delenv("SHOPIFY_SALES_ACCOUNT_SALES_ID", raising=False)
    monkeypatch.delenv("SHOPIFY_SALES_ACCOUNT_COMMISSION_ID", raising=False)
    with pytest.raises(ValueError):
        resolve_account_ids(account_items=[{"id": 1, "name": "売上高"}])
