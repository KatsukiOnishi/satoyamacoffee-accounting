from __future__ import annotations

from datetime import date

import pytest


def _row(**overrides):
    from accounting.connectors.attendance import SalaryRow

    defaults = dict(
        staff_id="staff1",
        employee_no="001",
        last_name="山田",
        first_name="太郎",
        department="店舗",
        work_days=22,
        work_min=9600,
        base_pay=250000,
        transport_pay=12000,
        income_tax=5000,
        resident_tax=10000,
        social_ins=38000,
        total_pay=262000,
        total_deduct=53000,
        net_pay=209000,
    )
    defaults.update(overrides)
    return SalaryRow(**defaults)


def _account_items():
    return [
        {"id": 1001, "name": "給与手当"},
        {"id": 1002, "name": "旅費交通費"},
        {"id": 1003, "name": "預り金"},
        {"id": 1004, "name": "普通預金"},
        {"id": 1099, "name": "その他"},
    ]


# ---- resolve_account_ids ----


def test_resolve_account_ids_finds_all_required():
    from accounting.tasks.payroll import resolve_account_ids

    ids = resolve_account_ids(_account_items())
    assert ids.salary == 1001
    assert ids.transport == 1002
    assert ids.deposit == 1003
    assert ids.bank == 1004


def test_resolve_account_ids_raises_if_missing():
    from accounting.tasks.payroll import resolve_account_ids

    items = [a for a in _account_items() if a["name"] != "預り金"]
    with pytest.raises(ValueError, match="預り金"):
        resolve_account_ids(items)


# ---- build_external_id ----


def test_build_external_id_format():
    from accounting.tasks.payroll import build_external_id

    assert build_external_id(2026, 5, "staffX") == "payroll-2026-05-staffX"
    assert build_external_id(2026, 12, "abc") == "payroll-2026-12-abc"


# ---- build_journal_payload ----


def _build(row, *, issue_date=date(2026, 5, 31)):
    from accounting.tasks.payroll import build_journal_payload, resolve_account_ids

    ids = resolve_account_ids(_account_items())
    return build_journal_payload(
        row=row, account_ids=ids, company_id=12345, issue_date=issue_date
    )


def test_build_journal_payload_balances():
    payload = _build(_row())
    debits = sum(d["amount"] for d in payload["details"] if d["entry_side"] == "debit")
    credits = sum(d["amount"] for d in payload["details"] if d["entry_side"] == "credit")
    assert debits == credits == 262000


def test_build_journal_payload_no_transport_when_zero():
    """transport_pay=0 のときは旅費交通費 detail を入れない。"""
    payload = _build(_row(transport_pay=0, base_pay=200000, total_pay=200000, total_deduct=53000, net_pay=147000))
    # 借方は給与手当だけ
    debits = [d for d in payload["details"] if d["entry_side"] == "debit"]
    assert len(debits) == 1
    assert debits[0]["account_item_id"] == 1001
    # 借方 = 貸方
    debit_sum = sum(d["amount"] for d in debits)
    credit_sum = sum(d["amount"] for d in payload["details"] if d["entry_side"] == "credit")
    assert debit_sum == credit_sum == 200000


def test_build_journal_payload_zero_deductions():
    """所得税・住民税・社保が全部 0 のとき、貸方は普通預金 1 件のみ。"""
    payload = _build(_row(
        income_tax=0, resident_tax=0, social_ins=0,
        total_pay=262000, total_deduct=0, net_pay=262000,
    ))
    credits = [d for d in payload["details"] if d["entry_side"] == "credit"]
    assert len(credits) == 1
    assert credits[0]["account_item_id"] == 1004  # 普通預金
    assert credits[0]["amount"] == 262000


def test_build_journal_payload_description_contains_name_and_period():
    payload = _build(_row(last_name="鈴木", first_name="花子"), issue_date=date(2026, 5, 31))
    descs = [d["description"] for d in payload["details"]]
    assert all("鈴木花子" in d for d in descs)
    assert all("2026-05月給与" in d for d in descs)


def test_build_journal_payload_unbalanced_raises():
    """totalPay と内訳の合計が一致しないデータには厳格に失敗させる。"""
    from accounting.tasks.payroll import build_journal_payload, resolve_account_ids

    ids = resolve_account_ids(_account_items())
    # base_pay + transport_pay = 250000 + 12000 = 262000
    # でも credits を強制的にずらすため net_pay を不整合に
    bad = _row(net_pay=999)  # 借方 262000 ≠ 貸方 (5000+10000+38000+999=53999)
    with pytest.raises(ValueError, match="一致しません"):
        build_journal_payload(
            row=bad, account_ids=ids, company_id=12345, issue_date=date(2026, 5, 31)
        )


# ---- _parse_month ----


def test_parse_month_valid():
    from accounting.tasks.payroll import _parse_month

    assert _parse_month("2026-05") == (2026, 5)
    assert _parse_month("2026-12") == (2026, 12)


def test_parse_month_invalid():
    from accounting.tasks.payroll import _parse_month

    with pytest.raises(ValueError):
        _parse_month("2026/05")
    with pytest.raises(ValueError):
        _parse_month("2026-13")
    with pytest.raises(ValueError):
        _parse_month("hoge")


# ---- SalaryRow.from_dict ----


def test_salary_row_from_dict_handles_missing_fields():
    from accounting.connectors.attendance import SalaryRow

    row = SalaryRow.from_dict({
        "staffId": "s1", "employeeNo": "001",
        "lastName": "山田", "firstName": "太郎",
        # department, work*, basePay 等は省略
    })
    assert row.staff_id == "s1"
    assert row.full_name == "山田 太郎"
    assert row.department is None
    assert row.base_pay == 0
    assert row.net_pay == 0
