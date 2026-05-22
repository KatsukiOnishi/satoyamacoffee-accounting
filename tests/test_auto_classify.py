"""auto-classify タスクの単体テスト。"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    from accounting.config import settings
    from accounting.core import db as db_module

    monkeypatch.setattr(settings, "database_path", str(tmp_path / "test_ac.db"))
    db_module._engine = None
    db_module._SessionLocal = None
    db_module.init_db()
    yield tmp_path
    db_module._engine = None
    db_module._SessionLocal = None


# ---- mode_manager ----


def test_mode_manager_default_is_shadow(isolated_db):
    from accounting.tasks.auto_classify import mode_manager

    assert mode_manager.get_mode() == "shadow"


def test_mode_manager_set_production_and_back(isolated_db):
    from accounting.tasks.auto_classify import mode_manager

    mode_manager.set_mode("production", reason="2週間で精度97%")
    assert mode_manager.get_mode() == "production"
    mode_manager.set_mode("shadow", reason="戻し")
    assert mode_manager.get_mode() == "shadow"


def test_mode_manager_invalid_mode_raises(isolated_db):
    from accounting.tasks.auto_classify import mode_manager

    with pytest.raises(ValueError):
        mode_manager.set_mode("bogus")


def test_thresholds_defaults(isolated_db):
    from accounting.tasks.auto_classify import mode_manager

    high, low = mode_manager.get_thresholds()
    assert high == pytest.approx(0.85)
    assert low == pytest.approx(0.6)


# ---- classifier (純粋関数部分) ----


def _txn(amount: int, desc: str, *, txn_id: int = 1):
    from accounting.tasks.auto_classify.models import WalletTxnForClassify

    return WalletTxnForClassify(
        id=txn_id,
        date=date(2026, 5, 1),
        description=desc,
        amount=amount,
        walletable_type="bank_account",
        walletable_id=100,
        walletable_name="普通預金",
        entry_side="expense" if amount < 0 else "income",
    )


def test_build_prompts_includes_masters_and_examples():
    from accounting.tasks.auto_classify import classifier as c

    txn = _txn(-9535, "YAMATOTRANSPORTCO.,LTD.")
    accs = [{"id": 1, "name": "荷造運賃"}, {"id": 2, "name": "消耗品費"}]
    partners = [{"id": 100, "name": "ヤマト運輸株式会社"}]
    examples = [
        {
            "type": "expense",
            "date": "2026-04-01",
            "partner_name": "ヤマト運輸",
            "amount": 9000,
            "description": "YAMATO",
            "account_item_name": "荷造運賃",
            "tax_name": "課対仕入10%",
        }
    ]
    system, user = c.build_prompts(
        txn, account_items=accs, partners=partners, past_examples=examples
    )
    assert "荷造運賃" in system
    assert "ヤマト運輸株式会社" in system
    assert "YAMATO" in system
    assert "YAMATOTRANSPORTCO" in user
    assert "出金(-)" in user


def test_resolve_masters_account_and_tax_partner():
    from accounting.tasks.ar_reconcile.matcher import normalize_partner_name
    from accounting.tasks.auto_classify import classifier as c
    from accounting.tasks.auto_classify.models import ClassifyWalletTxnOutput

    masters = {
        "account_items_by_name": {"荷造運賃": {"id": 7, "name": "荷造運賃"}},
        "tax_codes_by_name": {"課対仕入10%": {"code": 21, "name": "課対仕入10%"}},
        "partners_by_norm_name": {
            normalize_partner_name("ヤマト運輸株式会社"): {
                "id": 999,
                "name": "ヤマト運輸株式会社",
            }
        },
    }
    out = ClassifyWalletTxnOutput(
        account_item_name="荷造運賃",
        tax_code_name="課対仕入10%",
        partner_name="ヤマト運輸",  # 短縮形でも normalize で当たる
        confidence=0.92,
        reason="YAMATO は配送料",
    )
    ai_id, tax_id, partner_id = c.resolve_masters(out, masters)
    assert ai_id == 7
    assert tax_id == 21
    assert partner_id == 999


def test_resolve_masters_returns_none_when_unknown():
    from accounting.tasks.auto_classify import classifier as c
    from accounting.tasks.auto_classify.models import ClassifyWalletTxnOutput

    masters = {
        "account_items_by_name": {},
        "tax_codes_by_name": {},
        "partners_by_norm_name": {},
    }
    out = ClassifyWalletTxnOutput(
        account_item_name="未知科目",
        tax_code_name="未知税",
        partner_name=None,
        confidence=0.5,
        reason="-",
    )
    ai, tax, partner = c.resolve_masters(out, masters)
    assert ai is None
    assert tax is None
    assert partner is None


# ---- registrar: shadow ガード ----


def test_registrar_raises_on_shadow_mode():
    from accounting.tasks.auto_classify import registrar
    from accounting.tasks.auto_classify.models import ClassificationResult

    result = ClassificationResult(wallet_txn=_txn(-9535, "YAMATO"))
    fake_freee = MagicMock()
    with pytest.raises(registrar.ShadowModeViolation):
        registrar.register_deal_for_classification(
            fake_freee, result, company_id=3206591, mode="shadow", run_id="run-1"
        )
    fake_freee.create_deal.assert_not_called()


def test_registrar_review_required_when_account_item_not_resolved(isolated_db):
    """ai_id が None なら freee は叩かず review_required 返す。"""
    from accounting.tasks.auto_classify import registrar
    from accounting.tasks.auto_classify.models import (
        ClassificationResult,
        ClassifyWalletTxnOutput,
    )

    result = ClassificationResult(
        wallet_txn=_txn(-9535, "YAMATO"),
        output=ClassifyWalletTxnOutput(
            account_item_name="未知科目",
            tax_code_name="課対仕入10%",
            partner_name=None,
            confidence=0.95,
            reason="-",
        ),
        resolved_account_item_id=None,
        resolved_tax_code_id=21,
    )
    fake_freee = MagicMock()
    out = registrar.register_deal_for_classification(
        fake_freee, result, company_id=3206591, mode="production", run_id="run-1"
    )
    assert out.action_taken == "review_required"
    fake_freee.create_deal.assert_not_called()


def test_registrar_calls_freee_when_production_and_resolved(isolated_db):
    from accounting.tasks.auto_classify import registrar
    from accounting.tasks.auto_classify.models import (
        ClassificationResult,
        ClassifyWalletTxnOutput,
    )

    result = ClassificationResult(
        wallet_txn=_txn(-9535, "YAMATO"),
        output=ClassifyWalletTxnOutput(
            account_item_name="荷造運賃",
            tax_code_name="課対仕入10%",
            partner_name=None,
            confidence=0.95,
            reason="-",
        ),
        resolved_account_item_id=7,
        resolved_tax_code_id=21,
    )
    fake_freee = MagicMock()
    fake_freee.create_deal.return_value = {"deal_id": 12345}

    out = registrar.register_deal_for_classification(
        fake_freee, result, company_id=3206591, mode="production", run_id="run-1"
    )
    assert out.action_taken == "registered"
    assert out.freee_deal_id == 12345
    fake_freee.create_deal.assert_called_once()
    payload = fake_freee.create_deal.call_args.args[0]
    assert payload["type"] == "expense"
    assert payload["details"][0]["account_item_id"] == 7
    assert payload["details"][0]["tax_code"] == 21
    # wallet_txn の口座情報が payments に渡されている
    assert "payments" in payload
    assert payload["payments"][0]["from_walletable_type"] == "bank_account"
    assert payload["payments"][0]["from_walletable_id"] == 100
    # 符号: amount は abs にされる
    assert payload["details"][0]["amount"] == 9535


def test_registrar_idempotent_when_already_executed(isolated_db):
    from accounting.core.idempotency import mark_executed
    from accounting.tasks.auto_classify import registrar
    from accounting.tasks.auto_classify.models import (
        ClassificationResult,
        ClassifyWalletTxnOutput,
    )

    txn = _txn(-1000, "YAMATO", txn_id=999)
    mark_executed(
        registrar.TASK_NAME, registrar.build_external_id(999), "old-run", "deal-77", "success"
    )
    result = ClassificationResult(
        wallet_txn=txn,
        output=ClassifyWalletTxnOutput(
            account_item_name="荷造運賃",
            tax_code_name="課対仕入10%",
            partner_name=None,
            confidence=0.95,
            reason="-",
        ),
        resolved_account_item_id=7,
        resolved_tax_code_id=21,
    )
    fake_freee = MagicMock()
    out = registrar.register_deal_for_classification(
        fake_freee, result, company_id=3206591, mode="production", run_id="run-1"
    )
    assert out.action_taken == "registered"
    fake_freee.create_deal.assert_not_called()
