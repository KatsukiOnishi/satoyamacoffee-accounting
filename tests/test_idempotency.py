from __future__ import annotations

import uuid

import pytest


@pytest.fixture
def isolated_db(tmp_path):
    """テストごとに独立した SQLite ファイルを使う。

    モジュール reload は SQLAlchemy の Base/metadata と衝突するため、
    settings.database_path を一時的に差し替えて engine キャッシュだけ作り直す。
    """
    from accounting.config import settings
    from accounting.core import db as db_module
    from accounting.core import idempotency as idem_module

    original_path = settings.database_path
    settings.database_path = str(tmp_path / "test_accounting.db")
    db_module._engine = None
    db_module._SessionLocal = None
    db_module.init_db()

    try:
        yield idem_module
    finally:
        settings.database_path = original_path
        db_module._engine = None
        db_module._SessionLocal = None


def test_is_executed_returns_false_when_no_record(isolated_db):
    idem = isolated_db
    assert idem.is_executed("test_task", f"ext-{uuid.uuid4().hex[:8]}") is False


def test_mark_success_then_is_executed(isolated_db):
    idem = isolated_db
    task, eid = "test_task", f"ext-{uuid.uuid4().hex[:8]}"
    idem.mark_executed(task, eid, "run-1", "j-1", "success")
    assert idem.is_executed(task, eid) is True


def test_failed_status_does_not_block_retry(isolated_db):
    """status=failed のレコードがあっても is_executed は False（再実行可能）。"""
    idem = isolated_db
    task, eid = "test_task", f"ext-{uuid.uuid4().hex[:8]}"
    idem.mark_executed(task, eid, "run-1", None, "failed")
    assert idem.is_executed(task, eid) is False


def test_dry_run_status_does_not_block_real_run(isolated_db):
    """status=dry_run のレコードは本番実行をブロックしない。"""
    idem = isolated_db
    task, eid = "test_task", f"ext-{uuid.uuid4().hex[:8]}"
    idem.mark_executed(task, eid, "run-1", None, "dry_run")
    assert idem.is_executed(task, eid) is False


def test_mark_executed_upserts_on_retry(isolated_db):
    """failed → 再実行 → success でステータスが更新される。"""
    idem = isolated_db
    task, eid = "test_task", f"ext-{uuid.uuid4().hex[:8]}"
    idem.mark_executed(task, eid, "run-1", None, "failed")
    assert idem.is_executed(task, eid) is False
    idem.mark_executed(task, eid, "run-2", "j-2", "success")
    assert idem.is_executed(task, eid) is True

    rec = idem.get_execution(task, eid)
    assert rec is not None
    assert rec["run_id"] == "run-2"
    assert rec["freee_journal_id"] == "j-2"
    assert rec["status"] == "success"


def test_get_execution_returns_none_when_missing(isolated_db):
    idem = isolated_db
    assert idem.get_execution("test_task", f"ext-{uuid.uuid4().hex[:8]}") is None
