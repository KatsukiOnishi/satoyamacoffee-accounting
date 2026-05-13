"""inventory_valuation タスクのユニットテスト。

副作用のある統合パス（freee 本番 API / coffee_system 本番 API）はモックで遮断する。
"""
from __future__ import annotations

from datetime import date

import pytest


# ---- 純粋関数のテスト ----


def test_parse_month_valid():
    from accounting.tasks.inventory_valuation import _parse_month

    assert _parse_month("2026-04") == (2026, 4)
    assert _parse_month("2025-12") == (2025, 12)
    assert _parse_month("2026-01") == (2026, 1)


@pytest.mark.parametrize(
    "bad",
    ["2026/04", "26-04", "2026-13", "2026-00", "2026-1", "abcd-ef", "", "2026-04-30"],
)
def test_parse_month_invalid(bad):
    from accounting.tasks.inventory_valuation import _parse_month

    with pytest.raises(ValueError):
        _parse_month(bad)


def test_last_day():
    from accounting.tasks.inventory_valuation import _last_day

    assert _last_day(2026, 4) == date(2026, 4, 30)
    assert _last_day(2026, 2) == date(2026, 2, 28)
    assert _last_day(2024, 2) == date(2024, 2, 29)  # 閏年
    assert _last_day(2026, 12) == date(2026, 12, 31)


def test_previous_month_key():
    from accounting.core.inventory_valuations import previous_month_key

    assert previous_month_key("2026-04") == "2026-03"
    assert previous_month_key("2026-01") == "2025-12"
    assert previous_month_key("2025-03") == "2025-02"


def test_resolve_account_id_hit():
    from accounting.tasks.inventory_valuation import _resolve_account_id

    items = [
        {"id": 1, "name": "現金"},
        {"id": 42, "name": "商品"},
        {"id": 100, "name": "期末商品棚卸高"},
    ]
    assert _resolve_account_id(items, "商品") == 42
    assert _resolve_account_id(items, "期末商品棚卸高") == 100


def test_resolve_account_id_miss():
    from accounting.tasks.inventory_valuation import _resolve_account_id

    items = [{"id": 1, "name": "現金"}]
    with pytest.raises(ValueError, match="見つかりません"):
        _resolve_account_id(items, "商品")


# ---- payload 構造のテスト ----


def test_build_closing_payload_balances():
    from accounting.tasks.inventory_valuation import _build_closing_payload

    p = _build_closing_payload(
        company_id=12345,
        issue_date=date(2026, 4, 30),
        amount=1_000_000,
        inventory_aid=42,
        closing_inventory_aid=100,
        month="2026-04",
    )
    assert p["company_id"] == 12345
    assert p["issue_date"] == "2026-04-30"
    debits = [d for d in p["details"] if d["entry_side"] == "debit"]
    credits = [d for d in p["details"] if d["entry_side"] == "credit"]
    assert len(debits) == 1
    assert len(credits) == 1
    assert debits[0]["account_item_id"] == 42  # 商品
    assert credits[0]["account_item_id"] == 100  # 期末商品棚卸高
    assert debits[0]["amount"] == credits[0]["amount"] == 1_000_000


def test_build_reversal_payload_balances():
    from accounting.tasks.inventory_valuation import _build_reversal_payload

    p = _build_reversal_payload(
        company_id=12345,
        issue_date=date(2026, 4, 1),
        amount=900_000,
        inventory_aid=42,
        closing_inventory_aid=100,
        prev_month="2026-03",
    )
    assert p["issue_date"] == "2026-04-01"
    debits = [d for d in p["details"] if d["entry_side"] == "debit"]
    credits = [d for d in p["details"] if d["entry_side"] == "credit"]
    # 逆向き: (借) 期末商品棚卸高 / (貸) 商品
    assert debits[0]["account_item_id"] == 100
    assert credits[0]["account_item_id"] == 42
    assert debits[0]["amount"] == credits[0]["amount"] == 900_000


# ---- 統合フロー（モック）のテスト ----


@pytest.fixture
def hub_env(monkeypatch, tmp_path):
    """テスト用の最低限の env と一時 SQLite を用意。"""
    monkeypatch.setenv("FREEE_COMPANY_ID", "12645899")
    monkeypatch.setenv("FREEE_API_KEY", "dummy-token")
    monkeypatch.setenv("COFFEE_SYSTEM_BASE_URL", "http://example.invalid")
    monkeypatch.setenv("COFFEE_SYSTEM_API_KEY", "dummy")
    from accounting.config import settings as cfg
    from accounting.core import db as db_module

    original_path = cfg.database_path
    cfg.database_path = str(tmp_path / "test.db")
    db_module._engine = None
    db_module._SessionLocal = None
    yield
    cfg.database_path = original_path
    db_module._engine = None
    db_module._SessionLocal = None


def test_run_dry_run_first_month_no_previous(mocker, hub_env):
    """初回実行（前月履歴なし）の dry-run。当月計上 1 本だけプレビューされる。"""
    from accounting.core.dry_run import DryRunContext
    from accounting.tasks import inventory_valuation as task

    # coffee_system モック
    mock_get = mocker.patch.object(
        task.CoffeeSystemClient,
        "get_inventory_value",
        return_value={"as_of": "2026-04-30", "total_jpy": 1_000_000},
    )
    mocker.patch.object(task.CoffeeSystemClient, "__init__", return_value=None)
    mocker.patch.object(task.CoffeeSystemClient, "__enter__", lambda self: self)
    mocker.patch.object(task.CoffeeSystemClient, "__exit__", lambda *a, **kw: None)

    # freee マスタモック
    mocker.patch.object(
        task.FreeeClient,
        "get_account_items",
        return_value=[
            {"id": 42, "name": "商品"},
            {"id": 100, "name": "期末商品棚卸高"},
        ],
    )
    mocker.patch.object(task.FreeeClient, "__init__", return_value=None)
    mocker.patch.object(task.FreeeClient, "__enter__", lambda self: self)
    mocker.patch.object(task.FreeeClient, "__exit__", lambda *a, **kw: None)

    # create_manual_journal は呼ばれないはず（dry-run）
    spy_create = mocker.patch.object(task.FreeeClient, "create_manual_journal")

    with DryRunContext(True):
        report = task.run(month="2026-04")

    mock_get.assert_called_once()
    spy_create.assert_not_called()  # dry-run なので実 API は叩かない
    assert report.failure_count == 0
    assert report.success_count == 1


def test_run_dry_run_with_previous_month(mocker, hub_env):
    """前月履歴ありの dry-run。逆仕訳と当月計上の両方プレビューされる。"""
    from accounting.core.db import init_db
    from accounting.core import inventory_valuations as iv_store
    from accounting.core.dry_run import DryRunContext
    from accounting.tasks import inventory_valuation as task

    init_db()
    # 前月の履歴を事前に投入
    iv_store.upsert(
        month="2026-03",
        amount_jpy=900_000,
        as_of=date(2026, 3, 31),
        run_id="seed",
        journal_id_closing="seed-closing",
    )

    mocker.patch.object(
        task.CoffeeSystemClient,
        "get_inventory_value",
        return_value={"as_of": "2026-04-30", "total_jpy": 1_000_000},
    )
    mocker.patch.object(task.CoffeeSystemClient, "__init__", return_value=None)
    mocker.patch.object(task.CoffeeSystemClient, "__enter__", lambda self: self)
    mocker.patch.object(task.CoffeeSystemClient, "__exit__", lambda *a, **kw: None)

    mocker.patch.object(
        task.FreeeClient,
        "get_account_items",
        return_value=[
            {"id": 42, "name": "商品"},
            {"id": 100, "name": "期末商品棚卸高"},
        ],
    )
    mocker.patch.object(task.FreeeClient, "__init__", return_value=None)
    mocker.patch.object(task.FreeeClient, "__enter__", lambda self: self)
    mocker.patch.object(task.FreeeClient, "__exit__", lambda *a, **kw: None)

    spy_create = mocker.patch.object(task.FreeeClient, "create_manual_journal")

    with DryRunContext(True):
        report = task.run(month="2026-04")

    spy_create.assert_not_called()
    assert report.failure_count == 0
    assert report.warning_count == 0  # 前月履歴あるので警告なし


def test_run_skips_when_already_executed(mocker, hub_env):
    """同月で既に実行済みなら、何もせず warning だけ返す。"""
    from accounting.core.db import init_db
    from accounting.core.dry_run import DryRunContext
    from accounting.core.idempotency import mark_executed
    from accounting.tasks import inventory_valuation as task

    init_db()
    mark_executed("inventory_valuation", "2026-04", "seed-run", "seed-journal", "success")

    # coffee_system / freee は呼ばれないはず
    cs_spy = mocker.patch.object(task.CoffeeSystemClient, "get_inventory_value")
    freee_spy = mocker.patch.object(task.FreeeClient, "create_manual_journal")

    with DryRunContext(True):
        report = task.run(month="2026-04")

    cs_spy.assert_not_called()
    freee_spy.assert_not_called()
    assert report.warning_count == 1
    assert report.success_count == 0
    assert report.failure_count == 0


def test_run_with_amount_override(mocker, hub_env):
    """--amount で coffee_system を経由せず直接金額指定。"""
    from accounting.core.dry_run import DryRunContext
    from accounting.tasks import inventory_valuation as task

    cs_spy = mocker.patch.object(task.CoffeeSystemClient, "get_inventory_value")

    mocker.patch.object(
        task.FreeeClient,
        "get_account_items",
        return_value=[
            {"id": 42, "name": "商品"},
            {"id": 100, "name": "期末商品棚卸高"},
        ],
    )
    mocker.patch.object(task.FreeeClient, "__init__", return_value=None)
    mocker.patch.object(task.FreeeClient, "__enter__", lambda self: self)
    mocker.patch.object(task.FreeeClient, "__exit__", lambda *a, **kw: None)
    mocker.patch.object(task.FreeeClient, "create_manual_journal")

    with DryRunContext(True):
        report = task.run(month="2026-04", amount_override=2_500_000)

    cs_spy.assert_not_called()  # override されたので coffee_system は呼ばない
    assert report.failure_count == 0


def test_run_fails_when_account_missing(mocker, hub_env):
    """freee マスタに勘定科目がなければ failure になる。"""
    from accounting.core.dry_run import DryRunContext
    from accounting.tasks import inventory_valuation as task

    mocker.patch.object(
        task.CoffeeSystemClient,
        "get_inventory_value",
        return_value={"as_of": "2026-04-30", "total_jpy": 1_000_000},
    )
    mocker.patch.object(task.CoffeeSystemClient, "__init__", return_value=None)
    mocker.patch.object(task.CoffeeSystemClient, "__enter__", lambda self: self)
    mocker.patch.object(task.CoffeeSystemClient, "__exit__", lambda *a, **kw: None)

    # 「商品」勘定科目がない状態
    mocker.patch.object(
        task.FreeeClient,
        "get_account_items",
        return_value=[{"id": 1, "name": "現金"}],
    )
    mocker.patch.object(task.FreeeClient, "__init__", return_value=None)
    mocker.patch.object(task.FreeeClient, "__enter__", lambda self: self)
    mocker.patch.object(task.FreeeClient, "__exit__", lambda *a, **kw: None)

    with DryRunContext(True):
        report = task.run(month="2026-04")

    assert report.failure_count == 1
