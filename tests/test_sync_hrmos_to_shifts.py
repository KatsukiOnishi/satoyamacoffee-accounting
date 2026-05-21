"""sync-hrmos タスクのユニットテスト。

HrmosClient と ShiftsAdminClient は丸ごとモックし、タスクのフロー制御だけを検証する。
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    from accounting.config import settings

    monkeypatch.setattr(settings, "database_path", str(tmp_path / "test_sync.db"))
    # Settings はキャッシュされない（毎回 settings 経由でアクセス）ので、
    # core/db のグローバル engine をリセットする
    from accounting.core import db as db_mod

    db_mod._engine = None  # type: ignore[attr-defined]
    db_mod._SessionLocal = None  # type: ignore[attr-defined]
    yield


@pytest.fixture
def env_set(monkeypatch):
    from accounting.config import settings

    monkeypatch.setattr(settings, "hrmos_login_url", "https://f.ieyasu.co/asd2171/login")
    monkeypatch.setattr(settings, "hrmos_user", "tester")
    monkeypatch.setattr(settings, "hrmos_pass", "secret")
    monkeypatch.setattr(settings, "hrmos_exclude_user_ids", "")
    monkeypatch.setattr(settings, "shifts_base_url", "https://shifts.test")
    monkeypatch.setattr(settings, "shifts_admin_api_key", "ADMIN_KEY")
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    yield


def test_previous_month_yyyymm():
    from accounting.tasks.sync_hrmos_to_shifts import previous_month_yyyymm

    assert previous_month_yyyymm(date(2026, 5, 21)) == "2026-04"
    assert previous_month_yyyymm(date(2026, 1, 5)) == "2025-12"
    assert previous_month_yyyymm(date(2026, 3, 1)) == "2026-02"


def test_invalid_month_raises(isolated_db, env_set):
    from accounting.tasks import sync_hrmos_to_shifts

    with pytest.raises(ValueError, match="YYYY-MM"):
        sync_hrmos_to_shifts.run(month="2026/04", dry_run=True)


def _fake_hrmos_csv(user_id: int, yyyymm: str = "2026-04"):
    from accounting.connectors.hrmos import HrmosCsv

    return HrmosCsv(
        user_id=user_id,
        yyyymm=yyyymm,
        filename=f"hrmos_{yyyymm}_{user_id}.csv",
        content=b"\xef\xbb\xbfheader1,header2\n2026-04-01,row1\n",
    )


def _staffs(*pairs):
    from accounting.connectors.hrmos import HrmosStaff

    return [HrmosStaff(user_id=uid, name=name) for uid, name in pairs]


def test_dry_run_full_flow(isolated_db, env_set):
    """dry-run: /staffs から全員列挙 → CSV取得 → shifts はモック呼び出しのみ、冪等性は dry_run 記録。"""
    from accounting.tasks import sync_hrmos_to_shifts

    fake_hrmos = MagicMock()
    fake_hrmos.__enter__.return_value = fake_hrmos
    fake_hrmos.list_active_staffs.return_value = _staffs(
        (2, "大西克直"), (7, "千葉脩斗"), (8, "保坂君夏")
    )
    fake_hrmos.download_csv.side_effect = lambda yyyymm, uid: _fake_hrmos_csv(uid, yyyymm)

    fake_shifts = MagicMock()
    fake_shifts.__enter__.return_value = fake_shifts
    from accounting.connectors.shifts import ShiftsImportResponse

    fake_shifts.import_hrmos_csvs.return_value = ShiftsImportResponse(
        received=3, parsed_rows=0, saved=0, skipped=[], errors=[]
    )

    with (
        patch("accounting.tasks.sync_hrmos_to_shifts.HrmosClient", return_value=fake_hrmos),
        patch("accounting.tasks.sync_hrmos_to_shifts.ShiftsAdminClient", return_value=fake_shifts),
    ):
        result = sync_hrmos_to_shifts.run(month="2026-04", dry_run=True)

    fake_hrmos.login.assert_called_once()
    fake_hrmos.list_active_staffs.assert_called_once()
    fake_hrmos.list_user_ids_for_month.assert_not_called()
    assert fake_hrmos.download_csv.call_count == 3
    fake_shifts.import_hrmos_csvs.assert_called_once()
    sent_files = fake_shifts.import_hrmos_csvs.call_args.kwargs["files"]
    assert [name for name, _ in sent_files] == [
        "hrmos_2026-04_2.csv",
        "hrmos_2026-04_7.csv",
        "hrmos_2026-04_8.csv",
    ]

    from accounting.core.idempotency import get_execution

    rec = get_execution("sync-hrmos", "2026-04")
    assert rec is not None
    assert rec["status"] == "dry_run"

    assert result["summary"]["success_count"] == 3


def test_exclude_user_ids_filters_out_test_accounts(isolated_db, env_set, monkeypatch):
    from accounting.config import settings
    from accounting.tasks import sync_hrmos_to_shifts

    monkeypatch.setattr(settings, "hrmos_exclude_user_ids", "5,6")

    fake_hrmos = MagicMock()
    fake_hrmos.__enter__.return_value = fake_hrmos
    fake_hrmos.list_active_staffs.return_value = _staffs(
        (2, "大西"), (5, "承認太郎"), (6, "草野"), (7, "千葉"),
    )
    fake_hrmos.download_csv.side_effect = lambda yyyymm, uid: _fake_hrmos_csv(uid, yyyymm)

    fake_shifts = MagicMock()
    fake_shifts.__enter__.return_value = fake_shifts
    from accounting.connectors.shifts import ShiftsImportResponse

    fake_shifts.import_hrmos_csvs.return_value = ShiftsImportResponse(
        received=2, parsed_rows=0, saved=0, skipped=[], errors=[]
    )

    with (
        patch("accounting.tasks.sync_hrmos_to_shifts.HrmosClient", return_value=fake_hrmos),
        patch("accounting.tasks.sync_hrmos_to_shifts.ShiftsAdminClient", return_value=fake_shifts),
    ):
        result = sync_hrmos_to_shifts.run(month="2026-04", dry_run=True)

    # 5, 6 は除外され、2, 7 だけ download される
    called_uids = [call.args[1] for call in fake_hrmos.download_csv.call_args_list]
    assert called_uids == [2, 7]

    # 除外した2件は warning に並ぶ
    warnings = result["summary"]["warnings"]
    excluded_items = [w for w in warnings if "excluded" in (w["detail"] or "")]
    assert len(excluded_items) == 2


def test_empty_csv_is_warning_not_failure(isolated_db, env_set):
    """勤怠ゼロの社員（download_csv が「空」RuntimeError）は warning に降格して続行。"""
    from accounting.tasks import sync_hrmos_to_shifts

    def fake_download(yyyymm, uid):
        if uid == 8:
            raise RuntimeError(f"HRMOS CSV が空 (yyyymm={yyyymm}, user_id={uid})")
        return _fake_hrmos_csv(uid, yyyymm)

    fake_hrmos = MagicMock()
    fake_hrmos.__enter__.return_value = fake_hrmos
    fake_hrmos.list_active_staffs.return_value = _staffs((7, "千葉"), (8, "保坂"))
    fake_hrmos.download_csv.side_effect = fake_download

    fake_shifts = MagicMock()
    fake_shifts.__enter__.return_value = fake_shifts
    from accounting.connectors.shifts import ShiftsImportResponse

    fake_shifts.import_hrmos_csvs.return_value = ShiftsImportResponse(
        received=1, parsed_rows=0, saved=0, skipped=[], errors=[]
    )

    with (
        patch("accounting.tasks.sync_hrmos_to_shifts.HrmosClient", return_value=fake_hrmos),
        patch("accounting.tasks.sync_hrmos_to_shifts.ShiftsAdminClient", return_value=fake_shifts),
    ):
        result = sync_hrmos_to_shifts.run(month="2026-04", dry_run=True)

    assert result["summary"]["success_count"] == 1  # 7 のみ
    assert result["summary"]["failure_count"] == 0  # 8 は失敗ではなく warning
    assert any("勤怠データなし" in (w["detail"] or "") for w in result["summary"]["warnings"])


def test_idempotency_skips_already_executed(isolated_db, env_set):
    from accounting.core import idempotency
    from accounting.core.db import init_db

    init_db()
    idempotency.mark_executed(
        task="sync-hrmos",
        external_id="2026-04",
        run_id="prev-run",
        freee_journal_id=None,
        status="success",
    )

    from accounting.tasks import sync_hrmos_to_shifts

    fake_hrmos = MagicMock()
    fake_hrmos.__enter__.return_value = fake_hrmos
    fake_shifts = MagicMock()
    fake_shifts.__enter__.return_value = fake_shifts

    with (
        patch("accounting.tasks.sync_hrmos_to_shifts.HrmosClient", return_value=fake_hrmos),
        patch("accounting.tasks.sync_hrmos_to_shifts.ShiftsAdminClient", return_value=fake_shifts),
    ):
        result = sync_hrmos_to_shifts.run(month="2026-04", dry_run=True)

    fake_hrmos.login.assert_not_called()
    fake_shifts.import_hrmos_csvs.assert_not_called()
    assert result["summary"]["warning_count"] >= 1


def test_user_ids_skips_idempotency_and_list(isolated_db, env_set):
    """--user-ids 指定時: 冪等性チェックも /staffs 列挙もスキップ。"""
    from accounting.tasks import sync_hrmos_to_shifts

    fake_hrmos = MagicMock()
    fake_hrmos.__enter__.return_value = fake_hrmos
    fake_hrmos.download_csv.side_effect = lambda yyyymm, uid: _fake_hrmos_csv(uid, yyyymm)

    fake_shifts = MagicMock()
    fake_shifts.__enter__.return_value = fake_shifts
    from accounting.connectors.shifts import ShiftsImportResponse

    fake_shifts.import_hrmos_csvs.return_value = ShiftsImportResponse(
        received=1, parsed_rows=0, saved=0, skipped=[], errors=[]
    )

    with (
        patch("accounting.tasks.sync_hrmos_to_shifts.HrmosClient", return_value=fake_hrmos),
        patch("accounting.tasks.sync_hrmos_to_shifts.ShiftsAdminClient", return_value=fake_shifts),
    ):
        sync_hrmos_to_shifts.run(month="2026-04", dry_run=True, user_ids=[7])

    fake_hrmos.list_active_staffs.assert_not_called()
    fake_hrmos.list_user_ids_for_month.assert_not_called()
    fake_hrmos.download_csv.assert_called_once_with("2026-04", 7)

    from accounting.core.idempotency import get_execution

    # 限定実行では冪等性記録を残さない
    assert get_execution("sync-hrmos", "2026-04") is None


def test_no_active_staffs_warns(isolated_db, env_set):
    from accounting.tasks import sync_hrmos_to_shifts

    fake_hrmos = MagicMock()
    fake_hrmos.__enter__.return_value = fake_hrmos
    fake_hrmos.list_active_staffs.return_value = []

    fake_shifts = MagicMock()
    fake_shifts.__enter__.return_value = fake_shifts

    with (
        patch("accounting.tasks.sync_hrmos_to_shifts.HrmosClient", return_value=fake_hrmos),
        patch("accounting.tasks.sync_hrmos_to_shifts.ShiftsAdminClient", return_value=fake_shifts),
    ):
        result = sync_hrmos_to_shifts.run(month="2026-04", dry_run=True)

    fake_shifts.import_hrmos_csvs.assert_not_called()
    assert result["summary"]["warning_count"] >= 1
