from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    from accounting.config import settings
    from accounting.core import db as db_module

    monkeypatch.setattr(settings, "database_path", str(tmp_path / "test_jr.db"))
    db_module._engine = None
    db_module._SessionLocal = None
    db_module.init_db()
    yield tmp_path
    db_module._engine = None
    db_module._SessionLocal = None


# ---- 純粋関数のテスト ----


def test_normalize_description():
    from accounting.tasks.journal_rules import normalize_description

    assert normalize_description("クレジットカード請求 2026/3/15 アマゾン") == "クレジットカード請求 アマゾン"
    assert normalize_description("AWS USAGE 2026-03-01_2026-03-31") == "AWS USAGE"
    assert normalize_description(" 電気料金 令和8年3月分 ") == "電気料金 令和年月分"
    assert normalize_description("") == ""
    assert normalize_description(None) == ""
    # 20文字で切られる
    assert len(normalize_description("あ" * 50)) == 20


def test_amount_band():
    from accounting.tasks.journal_rules import amount_band

    assert amount_band(0) == "0"
    assert amount_band(50) == "0-100"
    assert amount_band(500) == "100-1000"
    assert amount_band(5_000) == "1000-10000"
    assert amount_band(50_000) == "10000-100000"
    assert amount_band(500_000) == "100000-1M"
    assert amount_band(5_000_000) == "1M+"
    # 符号を無視
    assert amount_band(-50_000) == "10000-100000"


def test_grouping_by_partner_keyword_amount():
    from accounting.tasks.journal_rules import group_deal_details

    deals = [
        {
            "id": 1,
            "type": "expense",
            "partner_name": "アマゾン",
            "details": [
                {
                    "description": "アマゾン購入 2026/3/1",
                    "amount": 5000,
                    "account_item_name": "消耗品費",
                    "tax_name": "課対仕入 10%",
                }
            ],
        },
        {
            "id": 2,
            "type": "expense",
            "partner_name": "アマゾン",
            "details": [
                {
                    "description": "アマゾン購入 2026/4/2",
                    "amount": 7000,
                    "account_item_name": "消耗品費",
                    "tax_name": "課対仕入 10%",
                }
            ],
        },
        {
            "id": 3,
            "type": "expense",
            "partner_name": "楽天",
            "details": [
                {
                    "description": "楽天購入 2026/4/3",
                    "amount": 5000,
                    "account_item_name": "消耗品費",
                    "tax_name": "課対仕入 10%",
                }
            ],
        },
    ]
    groups = group_deal_details(deals)
    # GroupKey は (partner_name, keyword, entry_side) — 金額帯はグルーピング軸から除外済
    amazon_key = ("アマゾン", "アマゾン購入", "expense")
    rakuten_key = ("楽天", "楽天購入", "expense")
    assert len(groups[amazon_key]) == 2
    assert len(groups[rakuten_key]) == 1


def test_candidate_filtering_min_occurrence():
    from accounting.tasks.journal_rules import extract_candidates

    groups = {
        ("A", "kw_a", "expense"): [
            {"account_item_name": "消耗品費", "tax_name": "課対仕入 10%", "amount": 5000, "raw_description": "kw_a"}
            for _ in range(2)
        ],
        ("B", "kw_b", "expense"): [
            {"account_item_name": "消耗品費", "tax_name": "課対仕入 10%", "amount": 5000, "raw_description": "kw_b"}
            for _ in range(3)
        ],
    }
    out = extract_candidates(groups, min_occurrence=3, consistency_threshold=1.0)
    assert {c.keyword for c in out} == {"kw_b"}


def test_candidate_filtering_consistency():
    """勘定科目がバラバラ（一貫率 < 閾値）は除外される。"""
    from accounting.tasks.journal_rules import extract_candidates

    mixed = [
        {"account_item_name": "消耗品費", "tax_name": "課対仕入 10%", "amount": 5000, "raw_description": "x"},
        {"account_item_name": "消耗品費", "tax_name": "課対仕入 10%", "amount": 5000, "raw_description": "x"},
        {"account_item_name": "通信費", "tax_name": "課対仕入 10%", "amount": 5000, "raw_description": "x"},
    ]
    groups_low = {("P", "kw", "expense"): mixed}
    out_low = extract_candidates(groups_low, min_occurrence=3, consistency_threshold=1.0)
    assert out_low == []  # 一貫率 2/3 ≈ 0.67 < 1.0

    out_lower = extract_candidates(groups_low, min_occurrence=3, consistency_threshold=0.5)
    assert len(out_lower) == 1
    assert out_lower[0].suggested_account_item_name == "消耗品費"  # 最頻
    assert out_lower[0].consistency == pytest.approx(2 / 3, rel=1e-3)


def test_apply_skips_duplicates(isolated_db):
    """既存 matcher と (description, condition, entry_side_str) で衝突 → skip。"""
    from accounting.tasks.journal_rules import RuleCandidate, apply_rule_candidates

    candidate = RuleCandidate(
        keyword="アマゾン購入",
        condition=0,
        entry_side_str="expense",
        partner_name="アマゾン",
        suggested_account_item_name="消耗品費",
        suggested_tax_name="課対仕入 10%",
        act=0,
        occurrence=10,
        consistency=1.0,
    )
    existing = [
        {
            "id": 999,
            "description": "アマゾン購入",
            "condition": 0,
            "entry_side_str": "expense",
        }
    ]
    freee = MagicMock()
    freee.create_user_matcher.return_value = {"id": 1, "dry_run": False}

    res = apply_rule_candidates(
        [candidate],
        existing_matchers=existing,
        valid_account_item_names={"消耗品費"},
        freee=freee,
        run_id="test-run",
    )
    assert len(res.skipped_duplicates) == 1
    assert res.skipped_duplicates[0]["reason"] == "duplicate in freee"
    assert res.created == []
    freee.create_user_matcher.assert_not_called()


def test_apply_dry_run_no_api_call(isolated_db):
    """dry-run 時に freee.create_user_matcher は dry_run=True を返す形でしか呼ばれず、
    mark_executed もされない（rehearsal は本番 idempotency に混ぜない）。"""
    from accounting.core import idempotency
    from accounting.core.dry_run import DryRunContext
    from accounting.tasks.journal_rules import RuleCandidate, apply_rule_candidates

    candidate = RuleCandidate(
        keyword="新キーワード",
        condition=0,
        entry_side_str="expense",
        partner_name=None,
        suggested_account_item_name="消耗品費",
        suggested_tax_name="課対仕入 10%",
        act=0,
        occurrence=5,
        consistency=1.0,
    )

    # FreeeClient を実物に近い形で mock: dry_run コンテキスト中は dry_run レスポンスを返す
    from accounting.connectors.freee import FreeeClient

    real_freee = FreeeClient(api_key="x", company_id="0")
    # 本物の dry-run 判定を経由させたいので Client 自体は変えず、create_user_matcher だけ通常実装に任せる
    # （DryRunContext 下では実 API を叩かず "dry_run": True を返す実装）

    with DryRunContext(True):
        res = apply_rule_candidates(
            [candidate],
            existing_matchers=[],
            valid_account_item_names={"消耗品費"},
            freee=real_freee,
            run_id="test-run",
        )

    assert len(res.created) == 1
    assert res.created[0]["dry_run"] is True
    # mark_executed されていない（success が記録されない）
    external_id = res.created[0]["external_id"]
    assert idempotency.is_executed("journal_rules", external_id) is False
    real_freee.close()


def test_apply_account_item_validation(isolated_db):
    """freee に存在しない勘定科目は failed に積まれる。"""
    from accounting.tasks.journal_rules import RuleCandidate, apply_rule_candidates

    candidate = RuleCandidate(
        keyword="kw",
        condition=0,
        entry_side_str="expense",
        partner_name=None,
        suggested_account_item_name="存在しない科目",
        suggested_tax_name="",
        act=0,
        occurrence=5,
        consistency=1.0,
    )
    freee = MagicMock()

    res = apply_rule_candidates(
        [candidate],
        existing_matchers=[],
        valid_account_item_names={"消耗品費"},  # "存在しない科目" は含まれない
        freee=freee,
        run_id="test-run",
    )
    assert len(res.failed) == 1
    assert "account_item_name not found" in res.failed[0]["reason"]
    assert res.created == []
    freee.create_user_matcher.assert_not_called()


def test_collect_wallet_txn_with_deal_basic():
    """wallet_txn と deals.payments の (type, id, date, amount) で紐づく → 結合レコード。"""
    from accounting.tasks.journal_rules import collect_wallet_txn_with_deal

    wt = {
        "id": 1,
        "walletable_type": "credit_card",
        "walletable_id": 100,
        "date": "2026-03-01",
        "amount": -3000,
        "description": "アマゾン購入 2026/03/01",
        "entry_side": "expense",
    }
    deal = {
        "id": 10,
        "partner_id": 200,
        "type": "expense",
        "details": [
            {"account_item_id": 50, "tax_code": 5, "amount": -3000, "description": ""}
        ],
        "payments": [
            {
                "from_walletable_type": "credit_card",
                "from_walletable_id": 100,
                "date": "2026-03-01",
                "amount": -3000,
            }
        ],
    }
    records = collect_wallet_txn_with_deal(
        [wt],
        [deal],
        partner_map={200: "アマゾン"},
        account_item_map={50: "消耗品費"},
    )
    assert len(records) == 1
    r = records[0]
    assert r["description"] == "アマゾン購入 2026/03/01"
    assert r["partner_name"] == "アマゾン"
    assert r["account_item_name"] == "消耗品費"
    assert r["entry_side"] == "expense"
    assert r["walletable_type"] == "credit_card"
    assert r["deal_id"] == 10


def test_collect_wallet_txn_unmatched_skipped():
    """紐づく deal が無い wallet_txn はスキップされる。"""
    from accounting.tasks.journal_rules import collect_wallet_txn_with_deal

    wt = {
        "id": 1,
        "walletable_type": "credit_card",
        "walletable_id": 100,
        "date": "2026-03-01",
        "amount": -3000,
        "description": "未確定明細",
        "entry_side": "expense",
    }
    # 同日同額でも walletable_id が違うので紐づかない
    deal_other = {
        "id": 99,
        "partner_id": 0,
        "type": "expense",
        "details": [{"account_item_id": 1, "amount": -3000}],
        "payments": [
            {
                "from_walletable_type": "credit_card",
                "from_walletable_id": 999,  # ← 違う
                "date": "2026-03-01",
                "amount": -3000,
            }
        ],
    }
    records = collect_wallet_txn_with_deal([wt], [deal_other])
    assert records == []


def test_extract_candidates_with_wallet_txn_source():
    """wallet_txns 由来のレコードから expense 候補が抽出される。"""
    from accounting.tasks.journal_rules import (
        extract_candidates,
        group_wallet_txn_records,
    )

    records = [
        {
            "deal_id": 1,
            "description": "アマゾン購入 2026/03/01",
            "amount": -3000,
            "date": "2026-03-01",
            "entry_side": "expense",
            "partner_name": "アマゾン",
            "account_item_name": "消耗品費",
            "tax_name": "課対仕入 10%",
        },
        {
            "deal_id": 2,
            "description": "アマゾン購入 2026/03/15",
            "amount": -5000,
            "date": "2026-03-15",
            "entry_side": "expense",
            "partner_name": "アマゾン",
            "account_item_name": "消耗品費",
            "tax_name": "課対仕入 10%",
        },
        {
            "deal_id": 3,
            "description": "アマゾン購入 2026/04/02",
            "amount": -4000,
            "date": "2026-04-02",
            "entry_side": "expense",
            "partner_name": "アマゾン",
            "account_item_name": "消耗品費",
            "tax_name": "課対仕入 10%",
        },
    ]
    groups = group_wallet_txn_records(records)
    candidates = extract_candidates(groups, min_occurrence=2, consistency_threshold=1.0)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.keyword == "アマゾン購入"
    assert c.entry_side_str == "expense"
    assert c.suggested_account_item_name == "消耗品費"
    assert c.partner_name == "アマゾン"
    assert c.occurrence == 3


def test_entry_side_from_wallet_txn():
    """wallet_txn.entry_side が deal.type と異なっても、wallet_txn 側を優先採用する。

    user_matchers API のマッチング絞り込みは wallet_txn レベルで行われる仕様。
    freee の口座記法（クレカ口座=負債口座は残高増を income と記録）に従って
    wallet_txn.entry_side をそのまま使うことで、ルールが freee 側で確実に発火する。
    deal.type を優先採用すると、ルールは作成できても freee 側で適用されない事故が起きる。
    """
    from accounting.tasks.journal_rules import collect_wallet_txn_with_deal

    wt = {
        "id": 1,
        "walletable_type": "credit_card",
        "walletable_id": 100,
        "date": "2026-03-01",
        "amount": -3000,
        "description": "Vデビット ヤマト運輸",
        "entry_side": "income",  # ← クレカ口座視点では残高増 = income
    }
    deal = {
        "id": 10,
        "partner_id": 0,
        "type": "expense",  # ← 業務視点では expense（だが user_matcher の SoT ではない）
        "details": [{"account_item_id": 50, "amount": -3000}],
        "payments": [
            {
                "from_walletable_type": "credit_card",
                "from_walletable_id": 100,
                "date": "2026-03-01",
                "amount": -3000,
            }
        ],
    }
    records = collect_wallet_txn_with_deal(
        [wt], [deal], account_item_map={50: "荷造運賃"}
    )
    assert len(records) == 1
    assert records[0]["entry_side"] == "income"  # wallet_txn.entry_side 由来


def test_grouping_without_amount_band():
    """同一キーワードで金額違いの wallet_txn 群が1グループに集約される（band 軸廃止後）。"""
    from accounting.tasks.journal_rules import group_wallet_txn_records

    records = [
        {
            "deal_id": i,
            "description": "Vデビット YAMATOTRANSPOR",
            "amount": amt,
            "entry_side": "expense",
            "partner_name": "",
            "account_item_name": "荷造運賃",
            "tax_name": "",
        }
        for i, amt in enumerate([-500, -3000, -25000, -120000], start=1)
    ]
    groups = group_wallet_txn_records(records)
    # 金額が 500/3000/25000/120000 と band を跨いでいても 1 グループ
    assert len(groups) == 1
    key = ("", "Vデビット YAMATOTRANSPOR", "expense")
    assert len(groups[key]) == 4


def test_candidate_min_max_amount():
    """候補の min_amount/max_amount が全エントリ金額の min/max（絶対値）と一致する。"""
    from accounting.tasks.journal_rules import extract_candidates

    groups = {
        ("P", "kw", "expense"): [
            {"account_item_name": "荷造運賃", "tax_name": "", "amount": -500, "raw_description": "kw"},
            {"account_item_name": "荷造運賃", "tax_name": "", "amount": -3000, "raw_description": "kw"},
            {"account_item_name": "荷造運賃", "tax_name": "", "amount": -25000, "raw_description": "kw"},
        ],
    }
    out = extract_candidates(groups, min_occurrence=2, consistency_threshold=1.0)
    assert len(out) == 1
    c = out[0]
    assert c.min_amount == 500
    assert c.max_amount == 25000
    # _build_payload にも乗ること
    from accounting.tasks.journal_rules import _build_payload

    payload = _build_payload(c)
    assert payload["min_amount"] == 500
    assert payload["max_amount"] == 25000


def test_csv_roundtrip(tmp_path):
    """CSV 出力→読み込みでデータが保持される。"""
    from accounting.tasks.journal_rules import (
        RuleCandidate,
        candidates_to_csv,
        csv_to_candidates,
    )

    src = [
        RuleCandidate(
            keyword="kw1",
            condition=0,
            entry_side_str="expense",
            partner_name="アマゾン",
            suggested_account_item_name="消耗品費",
            suggested_tax_name="課対仕入 10%",
            act=0,
            occurrence=5,
            consistency=1.0,
            min_amount=500,
            max_amount=25000,
            sample_amounts=[5000, 6000, 7000],
            sample_descriptions=["a", "b", "c"],
        ),
        RuleCandidate(
            keyword="kw2",
            condition=0,
            entry_side_str="income",
            partner_name=None,
            suggested_account_item_name="売上高",
            suggested_tax_name="",
            act=0,
            occurrence=3,
            consistency=1.0,
        ),
    ]
    path = tmp_path / "out.csv"
    candidates_to_csv(src, path)
    loaded = csv_to_candidates(path)
    assert len(loaded) == 2
    assert loaded[0].keyword == "kw1"
    assert loaded[0].partner_name == "アマゾン"
    assert loaded[0].sample_amounts == [5000, 6000, 7000]
    assert loaded[0].sample_descriptions == ["a", "b", "c"]
    assert loaded[1].partner_name is None
    assert loaded[1].suggested_tax_name == ""
    # min/max がラウンドトリップで保持される（None も空文字経由で保持）
    assert loaded[0].min_amount == 500
    assert loaded[0].max_amount == 25000
    assert loaded[1].min_amount is None
    assert loaded[1].max_amount is None
