"""freee OAuth トークン管理（accounting/core/freee_auth.py）のユニットテスト。

実 API は叩かない。httpx の POST 部分のみモック。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest


@pytest.fixture
def isolated_token_file(tmp_path, monkeypatch):
    """テスト毎に独立した secrets/freee_tokens.json を使う。

    `settings.freee_token_file` を tmp_path 配下に差し替える。
    httpx の自動プロキシ検出（HTTP_PROXY/ALL_PROXY 等）をテスト時のみ無効化する
    （Cowork sandbox では SOCKS proxy が設定されており httpx.Client 構築に失敗するため）。
    """
    from accounting.config import settings

    original = settings.freee_token_file
    token_path = tmp_path / "secrets" / "freee_tokens.json"
    settings.freee_token_file = str(token_path)
    # client_id / secret も埋める
    settings.freee_client_id = "test_client_id"
    settings.freee_client_secret = "test_client_secret"
    # プロキシ環境変数を無効化（テスト用 httpx.Client がモック対象なので外部接続しない）
    for var in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        yield token_path
    finally:
        settings.freee_token_file = original


def _make_mock_response(status_code: int, json_data: dict) -> httpx.Response:
    """httpx.Response をモック生成する。"""
    return httpx.Response(
        status_code=status_code,
        json=json_data,
        request=httpx.Request("POST", "https://accounts.secure.freee.co.jp/public_api/token"),
    )


def test_bootstrap_creates_token_file(isolated_token_file):
    from accounting.core import freee_auth

    data = freee_auth.bootstrap(
        access_token="AT_INITIAL",
        refresh_token="RT_INITIAL",
        expires_in=21600,
    )
    assert isolated_token_file.exists()
    assert data["access_token"] == "AT_INITIAL"
    assert data["refresh_token"] == "RT_INITIAL"

    saved = json.loads(isolated_token_file.read_text(encoding="utf-8"))
    assert saved["access_token"] == "AT_INITIAL"
    assert saved["refresh_token"] == "RT_INITIAL"
    assert "expires_at" in saved
    assert "obtained_at" in saved


def test_get_access_token_returns_current_when_not_expired(isolated_token_file):
    from accounting.core import freee_auth

    freee_auth.bootstrap(
        access_token="AT_FRESH",
        refresh_token="RT_FRESH",
        expires_in=21600,
    )
    # refresh が走らないこと
    with patch("accounting.core.freee_auth._request_refresh") as mock_refresh:
        token = freee_auth.get_access_token()
        assert token == "AT_FRESH"
        mock_refresh.assert_not_called()


def test_get_access_token_triggers_refresh_when_near_expiry(isolated_token_file):
    """残り期限が REFRESH_MARGIN_SEC (5分) を切ったら自動 refresh。"""
    from accounting.core import freee_auth

    # 期限切れ寸前（残り 60秒）でブートストラップ
    freee_auth.bootstrap(
        access_token="AT_NEAR_EXPIRY",
        refresh_token="RT_NEAR_EXPIRY",
        expires_in=60,  # マージン 300 秒より小さい → 即 refresh
    )

    def fake_post(self, url, data=None, **kwargs):
        return _make_mock_response(
            200,
            {
                "access_token": "AT_NEW",
                "refresh_token": "RT_NEW",
                "expires_in": 21600,
                "token_type": "bearer",
            },
        )

    with patch.object(httpx.Client, "post", fake_post):
        token = freee_auth.get_access_token()

    assert token == "AT_NEW"
    # ファイル側も更新されていること
    saved = json.loads(isolated_token_file.read_text(encoding="utf-8"))
    assert saved["access_token"] == "AT_NEW"
    # refresh_token もローテーションされて保存されている（freee 仕様）
    assert saved["refresh_token"] == "RT_NEW"


def test_force_refresh_rotates_refresh_token(isolated_token_file):
    from accounting.core import freee_auth

    freee_auth.bootstrap(
        access_token="AT_OLD",
        refresh_token="RT_OLD",
        expires_in=10,  # 即 refresh 対象
    )

    def fake_post(self, url, data=None, **kwargs):
        # サーバが送ってくる refresh_token は OLD と異なる
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "RT_OLD"
        return _make_mock_response(
            200,
            {
                "access_token": "AT_ROTATED",
                "refresh_token": "RT_ROTATED",
                "expires_in": 21600,
            },
        )

    with patch.object(httpx.Client, "post", fake_post):
        data = freee_auth.force_refresh()

    assert data["access_token"] == "AT_ROTATED"
    assert data["refresh_token"] == "RT_ROTATED"


def test_invalid_grant_raises_specific_error(isolated_token_file):
    from accounting.core import freee_auth

    freee_auth.bootstrap(
        access_token="AT_OLD",
        refresh_token="RT_REVOKED",
        expires_in=10,
    )

    def fake_post(self, url, data=None, **kwargs):
        return _make_mock_response(
            400,
            {"error": "invalid_grant", "error_description": "Refresh token expired"},
        )

    with patch.object(httpx.Client, "post", fake_post):
        with pytest.raises(freee_auth.FreeeRefreshTokenInvalidError):
            freee_auth.force_refresh()


def test_missing_token_file_raises_bootstrap_required(isolated_token_file):
    from accounting.core import freee_auth

    # bootstrap せずに get_access_token を呼ぶ
    with pytest.raises(freee_auth.FreeeBootstrapRequiredError):
        freee_auth.get_access_token()


def test_status_masks_tokens(isolated_token_file):
    from accounting.core import freee_auth

    freee_auth.bootstrap(
        access_token="abcdefghijklmnop",
        refresh_token="zyxwvutsrqponmlk",
        expires_in=21600,
    )
    s = freee_auth.status()
    assert s["bootstrapped"] is True
    # 中身は伏せられている
    assert "abcdefghij" not in s["access_token_masked"]
    assert s["access_token_masked"].startswith("abcd")
    assert s["access_token_masked"].endswith("mnop (len=16)")
    assert isinstance(s["expires_in_seconds"], int)


def test_status_returns_unbootstrapped_when_missing(isolated_token_file):
    from accounting.core import freee_auth

    s = freee_auth.status()
    assert s["bootstrapped"] is False
    assert "path" in s


def test_atomic_write_doesnt_leave_partial_file_on_crash(isolated_token_file, monkeypatch):
    """json.dump 中に例外が起きても、本ファイルは未変更のまま。

    rename される前のテンポラリで失敗するため、元ファイルは保護される。
    """
    from accounting.core import freee_auth

    freee_auth.bootstrap(
        access_token="AT_SAFE",
        refresh_token="RT_SAFE",
        expires_in=21600,
    )
    original = isolated_token_file.read_text(encoding="utf-8")

    real_dump = json.dump

    def boom_dump(*args, **kwargs):
        real_dump(*args, **kwargs)
        raise RuntimeError("simulated crash after dump but before rename")

    # bootstrap を上書き呼びで失敗させる
    with patch("accounting.core.freee_auth.json.dump", side_effect=boom_dump):
        with pytest.raises(RuntimeError):
            freee_auth.bootstrap(
                access_token="AT_CORRUPT",
                refresh_token="RT_CORRUPT",
                expires_in=21600,
            )

    # 元ファイルは無傷
    after = isolated_token_file.read_text(encoding="utf-8")
    assert after == original


def test_build_authorize_url_contains_client_id(isolated_token_file):
    from accounting.core import freee_auth

    url = freee_auth.build_authorize_url()
    assert "client_id=test_client_id" in url
    assert "response_type=code" in url
    assert "prompt=select_company" in url


def test_concurrent_refresh_one_only_calls_endpoint(isolated_token_file):
    """既に refresh された後に force_refresh を呼んでも、再 refresh しないこと。

    ロックを取得した時点で「ファイルが新しい」と判明したら早期 return。
    """
    from accounting.core import freee_auth

    # まず古いトークンで bootstrap
    freee_auth.bootstrap(
        access_token="AT_OLD",
        refresh_token="RT_OLD",
        expires_in=10,  # 即 refresh 対象
    )

    # 1回目の refresh で「新しい」状態にする
    with patch.object(
        httpx.Client,
        "post",
        lambda self, url, data=None, **kw: _make_mock_response(
            200, {"access_token": "AT_NEW", "refresh_token": "RT_NEW", "expires_in": 21600}
        ),
    ):
        freee_auth.force_refresh()

    # 2回目の force_refresh は、ロック取得後に「もう新しい」と判定して skip するはず
    call_count = {"n": 0}

    def fake_post(self, url, data=None, **kwargs):
        call_count["n"] += 1
        return _make_mock_response(
            200, {"access_token": "AT_NEWER", "refresh_token": "RT_NEWER", "expires_in": 21600}
        )

    with patch.object(httpx.Client, "post", fake_post):
        data = freee_auth.force_refresh()

    assert call_count["n"] == 0, "新しいトークンがあれば refresh は走らないはず"
    assert data["access_token"] == "AT_NEW"  # 既存値のまま
