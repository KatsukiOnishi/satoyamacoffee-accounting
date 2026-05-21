"""HRMOS コネクタ (accounting/connectors/hrmos.py) のユニットテスト。

実 API は叩かない。httpx.Client の get/post をモックする。
"""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest


_LOGIN_HTML = """
<html><body>
<form action="/asd2171/login" method="post">
<input name="utf8" value="✓" />
<input type="hidden" name="authenticity_token" value="CSRF_TOKEN_12345" />
<input name="user[login_id]" />
<input name="user[password]" />
</form>
</body></html>
"""

_LOGGED_IN_HOME_HTML = "<html><body><h1>HRMOS ホーム</h1><a href='/logout'>logout</a></body></html>"

_BULK_APPROVALS_HTML = """
<html><body>
<table>
<tr><td><a href="/approvals/2/edit/works?date=2026-04">大西 克直</a></td></tr>
<tr><td><a href="/approvals/7/edit/works?date=2026-04">千葉 脩斗</a></td></tr>
<tr><td><a href="/approvals/8/edit/works?date=2026-04">保坂 君夏</a></td></tr>
<tr><td><a href="/approvals/2/edit/leaves?date=2026-04">(別画面)</a></td></tr>
</table>
</body></html>
"""

_STAFFS_HTML = """
<html><body><table>
<tr>
  <td class="cell02"><a href="/staffs/2">大西克直</a></td>
  <td><a href="/staffs/2/copy" class="btn">コピー登録</a></td>
</tr>
<tr>
  <td class="cell02"><a href="/staffs/5">承認太郎</a></td>
  <td><a href="/staffs/5/copy" class="btn">コピー登録</a></td>
</tr>
<tr>
  <td class="cell02"><a href="/staffs/15">森谷菜都美</a></td>
  <td><a href="/staffs/15/copy" class="btn">コピー登録</a></td>
</tr>
<tr>
  <td class="cell02"><a href="/staffs/7">千葉脩斗</a></td>
  <td><a href="/staffs/7/copy" class="btn">コピー登録</a></td>
</tr>
</table></body></html>
"""


@pytest.fixture
def hrmos_env(monkeypatch):
    from accounting.config import settings

    monkeypatch.setattr(settings, "hrmos_login_url", "https://f.ieyasu.co/asd2171/login")
    monkeypatch.setattr(settings, "hrmos_user", "tester")
    monkeypatch.setattr(settings, "hrmos_pass", "secret")
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    yield


def _resp(text: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        text=text,
        request=httpx.Request("GET", "https://f.ieyasu.co/dummy"),
    )


def _bytes_resp(content: bytes, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        content=content,
        request=httpx.Request("GET", "https://f.ieyasu.co/dummy"),
    )


def test_login_extracts_csrf_and_posts(hrmos_env):
    from accounting.connectors.hrmos import HrmosClient

    with HrmosClient() as client:
        with (
            patch.object(client._client, "get", return_value=_resp(_LOGIN_HTML)) as mock_get,
            patch.object(client._client, "post", return_value=_resp(_LOGGED_IN_HOME_HTML)) as mock_post,
        ):
            client.login()

        mock_get.assert_called_once_with("https://f.ieyasu.co/asd2171/login")
        _, kwargs = mock_post.call_args
        assert kwargs["data"]["authenticity_token"] == "CSRF_TOKEN_12345"
        assert kwargs["data"]["user[login_id]"] == "tester"
        assert kwargs["data"]["user[password]"] == "secret"
        assert client._logged_in is True


def test_login_raises_when_csrf_missing(hrmos_env):
    from accounting.connectors.hrmos import HrmosClient

    with HrmosClient() as client:
        with patch.object(client._client, "get", return_value=_resp("<html><body>no token</body></html>")):
            with pytest.raises(RuntimeError, match="authenticity_token"):
                client.login()


def test_login_raises_when_credentials_wrong(hrmos_env):
    """POST 後にログイン画面（authenticity_token + login_id 入力）が返ってきたら失敗扱い。"""
    from accounting.connectors.hrmos import HrmosClient

    with HrmosClient() as client:
        with (
            patch.object(client._client, "get", return_value=_resp(_LOGIN_HTML)),
            patch.object(client._client, "post", return_value=_resp(_LOGIN_HTML)),
        ):
            with pytest.raises(RuntimeError, match="ログインに失敗"):
                client.login()


def test_list_user_ids_for_month(hrmos_env):
    from accounting.connectors.hrmos import HrmosClient

    with HrmosClient() as client:
        client._logged_in = True  # login スキップ
        with patch.object(client._client, "get", return_value=_resp(_BULK_APPROVALS_HTML)) as mock_get:
            ids = client.list_user_ids_for_month("2026-04")

        assert ids == [2, 7, 8]  # 重複排除＋昇順
        args, kwargs = mock_get.call_args
        assert args[0] == "https://f.ieyasu.co/bulk_approvals"
        assert kwargs["params"] == {"date": "2026-04"}


def test_list_active_staffs_parses_staff_page(hrmos_env):
    from accounting.connectors.hrmos import HrmosClient, HrmosStaff

    with HrmosClient() as client:
        client._logged_in = True
        with patch.object(client._client, "get", return_value=_resp(_STAFFS_HTML)) as mock_get:
            staffs = client.list_active_staffs()

        # user_id 昇順、/copy リンクは除外、社員名が取れる
        assert staffs == [
            HrmosStaff(user_id=2, name="大西克直"),
            HrmosStaff(user_id=5, name="承認太郎"),
            HrmosStaff(user_id=7, name="千葉脩斗"),
            HrmosStaff(user_id=15, name="森谷菜都美"),
        ]
        args, _ = mock_get.call_args
        assert args[0] == "https://f.ieyasu.co/staffs"


def test_list_active_staffs_requires_login(hrmos_env):
    from accounting.connectors.hrmos import HrmosClient

    with HrmosClient() as client:
        with pytest.raises(RuntimeError, match="login"):
            client.list_active_staffs()


def test_download_csv_returns_raw_bytes(hrmos_env):
    from accounting.connectors.hrmos import HrmosClient

    csv_bytes = "日付,氏名\n2026-04-01,千葉 脩斗\n".encode("shift_jis")

    with HrmosClient() as client:
        client._logged_in = True
        with patch.object(client._client, "get", return_value=_bytes_resp(csv_bytes)) as mock_get:
            csv = client.download_csv("2026-04", 7)

        assert csv.content == csv_bytes
        assert csv.filename == "hrmos_2026-04_7.csv"
        assert csv.user_id == 7
        args, kwargs = mock_get.call_args
        assert args[0] == "https://f.ieyasu.co/works/csv_download"
        assert kwargs["params"] == {"date": "2026-04", "user_id": 7}


def test_download_csv_raises_when_empty(hrmos_env):
    from accounting.connectors.hrmos import HrmosClient

    with HrmosClient() as client:
        client._logged_in = True
        with patch.object(client._client, "get", return_value=_bytes_resp(b"")):
            with pytest.raises(RuntimeError, match="空"):
                client.download_csv("2026-04", 7)


def test_methods_require_login(hrmos_env):
    from accounting.connectors.hrmos import HrmosClient

    with HrmosClient() as client:
        with pytest.raises(RuntimeError, match="login"):
            client.list_user_ids_for_month("2026-04")
        with pytest.raises(RuntimeError, match="login"):
            client.download_csv("2026-04", 7)
