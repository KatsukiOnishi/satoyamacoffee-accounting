"""shifts (attendance-system 書き込み) コネクタのユニットテスト。"""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest


@pytest.fixture
def shifts_env(monkeypatch):
    from accounting.config import settings

    monkeypatch.setattr(settings, "shifts_base_url", "https://shifts.test")
    monkeypatch.setattr(settings, "shifts_admin_api_key", "ADMIN_KEY")
    monkeypatch.setattr(settings, "dry_run", False)
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    yield


def _json_resp(data: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=data,
        request=httpx.Request("POST", "https://shifts.test/api/admin/import-hrmos"),
    )


def test_import_dry_run_does_not_send(shifts_env, monkeypatch):
    """dry-run のときは httpx.post を一切呼ばない。"""
    from accounting.config import settings
    from accounting.connectors.shifts import ShiftsAdminClient

    monkeypatch.setattr(settings, "dry_run", True)
    # is_dry_run() は ContextVar なので env を変えるだけでは効かない。明示的に注入する。
    from accounting.core.dry_run import DryRunContext

    with DryRunContext(True), ShiftsAdminClient() as client:
        with patch.object(client._client, "post") as mock_post:
            result = client.import_hrmos_csvs(files=[("a.csv", b"x"), ("b.csv", b"yy")])

        mock_post.assert_not_called()
    assert result.received == 2
    assert result.saved == 0
    assert result.parsed_rows == 0


def test_import_posts_multipart_and_parses_response(shifts_env):
    from accounting.connectors.shifts import ShiftsAdminClient
    from accounting.core.dry_run import DryRunContext

    payload = {
        "received": 2,
        "parsedRows": 50,
        "saved": 48,
        "skipped": ["社員番号999（謎）はマスタ未登録"],
        "errors": [],
    }
    with DryRunContext(False), ShiftsAdminClient() as client:
        with patch.object(
            client._client,
            "post",
            return_value=_json_resp(payload),
        ) as mock_post:
            result = client.import_hrmos_csvs(
                files=[("hrmos_2026-04_7.csv", b"col1,col2\n"), ("hrmos_2026-04_8.csv", b"col1,col2\n")],
            )

        args, kwargs = mock_post.call_args
        assert args[0] == "https://shifts.test/api/admin/import-hrmos"
        sent = kwargs["files"]
        assert len(sent) == 2
        # 各 entry は ("files[]", (filename, content, mime))
        assert all(field == "files[]" for field, _ in sent)

    assert result.received == 2
    assert result.parsed_rows == 50
    assert result.saved == 48
    assert result.skipped == ["社員番号999（謎）はマスタ未登録"]
    assert result.has_warnings is True


def test_import_raises_on_non_200(shifts_env):
    from accounting.connectors.shifts import ShiftsAdminClient
    from accounting.core.dry_run import DryRunContext

    err_resp = httpx.Response(
        status_code=401,
        text="Unauthorized",
        request=httpx.Request("POST", "https://shifts.test/api/admin/import-hrmos"),
    )
    with DryRunContext(False), ShiftsAdminClient() as client:
        with patch.object(client._client, "post", return_value=err_resp):
            with pytest.raises(RuntimeError, match="status=401"):
                client.import_hrmos_csvs(files=[("a.csv", b"x")])


def test_import_rejects_empty_files(shifts_env):
    from accounting.connectors.shifts import ShiftsAdminClient

    with ShiftsAdminClient() as client:
        with pytest.raises(ValueError, match="files"):
            client.import_hrmos_csvs(files=[])


def test_constructor_requires_api_key(monkeypatch):
    from accounting.config import settings
    from accounting.connectors.shifts import ShiftsAdminClient

    monkeypatch.setattr(settings, "shifts_base_url", "https://shifts.test")
    monkeypatch.setattr(settings, "shifts_admin_api_key", "")
    with pytest.raises(ValueError, match="SHIFTS_ADMIN_API_KEY"):
        ShiftsAdminClient()
