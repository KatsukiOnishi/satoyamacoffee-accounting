"""Gmail OAuth トークン管理。

設計は `accounting.core.freee_auth` を踏襲する:
- access_token / refresh_token を `secrets/gmail_tokens.json` に保存
- 初回 bootstrap は `accounting auth gmail-init` から `InstalledAppFlow.run_local_server()` を起動
- 以降は API 呼び出し時に `google.oauth2.credentials.Credentials` が自動 refresh
- 失効時は再度 gmail-init を促す

scope: gmail.readonly のみ（送信権限は持たない）。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from accounting.config import settings
from accounting.core.logger import get_logger

logger = get_logger("gmail_auth")

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailAuthError(Exception):
    """Gmail 認証関連の基底エラー。"""


class GmailBootstrapRequiredError(GmailAuthError):
    """credentials.json or tokens.json が存在しない / 不正。

    呼び出し側は `accounting auth gmail-init` の実行を案内する。
    """


def _credentials_path() -> Path:
    raw = settings.gmail_credentials_file
    p = Path(raw).expanduser()
    if not p.is_absolute():
        repo_root = Path(__file__).resolve().parent.parent.parent
        p = (repo_root / p).resolve()
    return p


def _token_path() -> Path:
    raw = settings.gmail_token_file
    p = Path(raw).expanduser()
    if not p.is_absolute():
        repo_root = Path(__file__).resolve().parent.parent.parent
        p = (repo_root / p).resolve()
    return p


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as tf:
        tmp_path = Path(tf.name)
        json.dump(data, tf, ensure_ascii=False, indent=2, sort_keys=True)
        tf.write("\n")
        tf.flush()
        os.fsync(tf.fileno())
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)


def bootstrap_interactive() -> dict[str, Any]:
    """ローカル http サーバを上げてOAuth認可フローを実行する。

    Google の `InstalledAppFlow.run_local_server()` がブラウザを開き、
    ユーザーがGoogleアカウントで同意するとリダイレクトでコードを受け取り、
    内部で access_token / refresh_token を取得する。

    Returns: 保存したトークン dict
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds_path = _credentials_path()
    if not creds_path.exists():
        raise GmailBootstrapRequiredError(
            f"Google OAuth client_secret が見つかりません: {creds_path}\n"
            "Google Cloud Console でデスクトップアプリ用のOAuthクライアントを作成し、\n"
            f"client_secret_xxx.json を {creds_path} に配置してください。"
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), GMAIL_SCOPES)
    creds = flow.run_local_server(
        host="localhost",
        port=0,
        prompt="consent",
        open_browser=True,
    )
    data = json.loads(creds.to_json())
    _atomic_write(_token_path(), data)
    logger.info("gmail_auth.bootstrap.saved", path=str(_token_path()))
    return data


def get_credentials() -> Any:
    """google.oauth2.credentials.Credentials を返す。自動 refresh 込み。

    Raises:
        GmailBootstrapRequiredError: tokens.json 不在
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path = _token_path()
    if not token_path.exists():
        raise GmailBootstrapRequiredError(
            f"Gmail トークンファイルが存在しません: {token_path}\n"
            "`accounting auth gmail-init` を実行してください。"
        )
    creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
    if creds.expired and creds.refresh_token:
        logger.info("gmail_auth.refreshing")
        creds.refresh(Request())
        _atomic_write(_token_path(), json.loads(creds.to_json()))
        logger.info("gmail_auth.refreshed")
    return creds


def status() -> dict[str, Any]:
    """CLI 表示用の状態。"""
    token_path = _token_path()
    creds_path = _credentials_path()
    if not creds_path.exists():
        return {
            "bootstrapped": False,
            "reason": "credentials_file_missing",
            "credentials_path": str(creds_path),
            "token_path": str(token_path),
        }
    if not token_path.exists():
        return {
            "bootstrapped": False,
            "reason": "token_file_missing",
            "credentials_path": str(creds_path),
            "token_path": str(token_path),
        }
    return {
        "bootstrapped": True,
        "credentials_path": str(creds_path),
        "token_path": str(token_path),
    }
