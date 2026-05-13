"""freee OAuth トークン管理。

設計:
- access_token / refresh_token / expires_at を `secrets/freee_tokens.json` に保存
- atomic write（tmp + os.replace）でファイル破損を回避
- fcntl.flock で同時 refresh を排他制御（CLI と Web UI の並行実行対策）
- API 呼び出し時に期限切れ（残り REFRESH_MARGIN_SEC 以下）なら自動 refresh
- refresh 時に新しい refresh_token も保存（freee 仕様: 1回限り使用可能 + ローテーション必須）
- invalid_grant エラーは FreeeRefreshTokenInvalidError として上に投げる
  → CLI / タスク側で Resend 通知して再認可URLを案内
- bootstrap 未完了（JSON ファイル不在）なら FreeeBootstrapRequiredError
  → 呼び出し側は settings.freee_api_key にフォールバック（後方互換）

参考: https://developer.freee.co.jp/reference/authentication
  - expires_in: 21600 (6時間)
  - refresh_token は 1回限り、更新時に新しい refresh_token が発行される
  - 90日でリフレッシュトークン自体が失効
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import httpx

from accounting.config import settings
from accounting.core.logger import get_logger

logger = get_logger("freee_auth")

FREEE_TOKEN_ENDPOINT = "https://accounts.secure.freee.co.jp/public_api/token"
FREEE_AUTHORIZE_BASE = "https://accounts.secure.freee.co.jp/public_api/authorize"
ACCESS_TOKEN_LIFETIME_SEC = 21600  # 6時間（freee 仕様）
REFRESH_MARGIN_SEC = 300  # 5分前に先回りで refresh
HTTP_TIMEOUT_SEC = 30.0

# プロセス内ロック（同一プロセスから複数スレッドで叩かれた場合の保険）
_process_lock = threading.Lock()


class FreeeAuthError(Exception):
    """freee 認証関連の基底エラー。"""


class FreeeBootstrapRequiredError(FreeeAuthError):
    """トークンファイルが存在しない or 必須フィールドが揃っていない。

    呼び出し側は `accounting auth init` の実行を案内すること。
    """


class FreeeRefreshTokenInvalidError(FreeeAuthError):
    """refresh_token が無効（90日失効 or サーバ側無効化 or 競合で先に消費された）。

    呼び出し側は再認可URLを通知して人手対応を促すこと。
    """


def _token_file_path() -> Path:
    """設定された保管場所を Path で返す（プロジェクトルートからの相対も解決）。"""
    raw = settings.freee_token_file
    p = Path(raw).expanduser()
    if not p.is_absolute():
        # accounting/ パッケージ親（リポジトリルート）基準で解決
        repo_root = Path(__file__).resolve().parent.parent.parent
        p = (repo_root / p).resolve()
    return p


def _lock_file_path() -> Path:
    return _token_file_path().with_suffix(_token_file_path().suffix + ".lock")


@contextmanager
def _file_lock() -> Iterator[None]:
    """secrets/freee_tokens.json.lock に排他ロックを取る。

    プロセス内ロックも合わせて使い、同一プロセス内の複数スレッドも直列化する。
    """
    lock_path = _lock_file_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # touch
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with _process_lock:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """同じディレクトリに一時ファイルを書いて rename で原子的に置換する。

    部分書き込みや書き込み中のクラッシュで JSON が破損するのを防ぐ。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # 同じディレクトリ内に作らないと rename がデバイス跨ぎで失敗する
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


def _load_raw() -> dict[str, Any]:
    path = _token_file_path()
    if not path.exists():
        raise FreeeBootstrapRequiredError(
            f"トークンファイルが存在しません: {path}\n"
            "`accounting auth init --access-token <X> --refresh-token <Y>` を実行してください。"
        )
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise FreeeAuthError(f"トークンファイルが壊れています: {path} ({e})") from e
    for required in ("access_token", "refresh_token", "expires_at"):
        if not data.get(required):
            raise FreeeBootstrapRequiredError(
                f"トークンファイルに必須フィールド {required!r} がありません: {path}"
            )
    return data


def _parse_expires_at(s: str) -> datetime:
    # ISO8601、タイムゾーン込みで保存している前提
    return datetime.fromisoformat(s)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _is_expired_or_near(expires_at: datetime, margin_sec: int = REFRESH_MARGIN_SEC) -> bool:
    return _now_utc() >= (expires_at - timedelta(seconds=margin_sec))


def bootstrap(
    *,
    access_token: str,
    refresh_token: str,
    expires_in: int = ACCESS_TOKEN_LIFETIME_SEC,
    company_id: str | None = None,
) -> dict[str, Any]:
    """初回トークン投入。既存ファイルがあれば上書き。

    Args:
        access_token: freee Developer Console / curl で取得した access_token
        refresh_token: 同上の refresh_token（パスワードマネージャ管理）
        expires_in: トークンの有効期限（秒）。freee 標準は 21600
        company_id: 会社ID（任意、メタ情報として保存）

    Returns: 保存した内容（access_token は伏せずに返す。CLI 側でマスク）
    """
    obtained_at = _now_utc()
    expires_at = obtained_at + timedelta(seconds=expires_in)
    data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "obtained_at": obtained_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "company_id": company_id or settings.freee_company_id or "",
    }
    with _file_lock():
        _atomic_write(_token_file_path(), data)
    logger.info(
        "freee_auth.bootstrap.saved",
        path=str(_token_file_path()),
        expires_at=data["expires_at"],
    )
    return data


def _request_refresh(refresh_token: str) -> dict[str, Any]:
    """freee の token endpoint に refresh_token grant を投げる。

    Raises:
        FreeeRefreshTokenInvalidError: invalid_grant が返った
        FreeeAuthError: クライアント設定不足やネットワーク異常
    """
    if not settings.freee_client_id or not settings.freee_client_secret:
        raise FreeeAuthError(
            "FREEE_CLIENT_ID / FREEE_CLIENT_SECRET が .env に設定されていません。"
        )
    payload = {
        "grant_type": "refresh_token",
        "client_id": settings.freee_client_id,
        "client_secret": settings.freee_client_secret,
        "refresh_token": refresh_token,
    }
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_SEC) as client:
            resp = client.post(FREEE_TOKEN_ENDPOINT, data=payload)
    except httpx.HTTPError as e:
        raise FreeeAuthError(f"refresh リクエスト失敗（ネットワーク）: {e}") from e

    if resp.status_code in (400, 401):
        # freee の invalid_grant 等の判定
        try:
            err = resp.json()
        except ValueError:
            err = {"raw": resp.text}
        error_code = err.get("error")
        if error_code == "invalid_grant":
            logger.error(
                "freee_auth.refresh.invalid_grant",
                status=resp.status_code,
                body=err,
            )
            raise FreeeRefreshTokenInvalidError(
                f"refresh_token が無効です（invalid_grant）。"
                "90日経過 or 他プロセスで先に消費された可能性があります。"
                "再認可が必要: " + build_authorize_url()
            )
        logger.error(
            "freee_auth.refresh.client_error",
            status=resp.status_code,
            body=err,
        )
        raise FreeeAuthError(f"refresh が失敗: {resp.status_code} {err}")

    if not resp.is_success:
        logger.error(
            "freee_auth.refresh.server_error",
            status=resp.status_code,
            body=resp.text[:500],
        )
        raise FreeeAuthError(f"refresh が失敗: {resp.status_code} {resp.text[:200]}")

    return resp.json()


def force_refresh() -> dict[str, Any]:
    """ロックを取って refresh を実行し、ファイルを更新する。

    新しい refresh_token が発行されるのでそれも保存する（freee 仕様）。
    """
    with _file_lock():
        current = _load_raw()
        # ロック取得中に他プロセスが既に refresh していたら、その新しいトークンを使う
        # （重複 refresh で invalid_grant を踏むのを避ける）
        # ただし「自分が古いと判断したから force_refresh を呼んだ」ので、
        # 期限がまだ十分あればそのまま返す。
        expires_at = _parse_expires_at(current["expires_at"])
        if not _is_expired_or_near(expires_at):
            logger.info(
                "freee_auth.force_refresh.skipped",
                reason="another_process_refreshed",
                expires_at=current["expires_at"],
            )
            return current

        logger.info("freee_auth.refresh.start", expires_at=current["expires_at"])
        new_tokens = _request_refresh(current["refresh_token"])

        obtained_at = _now_utc()
        expires_in = int(new_tokens.get("expires_in") or ACCESS_TOKEN_LIFETIME_SEC)
        new_data = {
            "access_token": new_tokens["access_token"],
            # freee は更新時に新しい refresh_token を返す
            "refresh_token": new_tokens.get("refresh_token", current["refresh_token"]),
            "obtained_at": obtained_at.isoformat(),
            "expires_at": (obtained_at + timedelta(seconds=expires_in)).isoformat(),
            "company_id": current.get("company_id", ""),
        }
        _atomic_write(_token_file_path(), new_data)
        logger.info(
            "freee_auth.refresh.success",
            expires_at=new_data["expires_at"],
            rotated=new_data["refresh_token"] != current["refresh_token"],
        )
        return new_data


def get_access_token() -> str:
    """API 呼び出しが使う access_token を返す。必要なら自動 refresh。

    Raises:
        FreeeBootstrapRequiredError: 初回 bootstrap 未完了
        FreeeRefreshTokenInvalidError: refresh も失敗
        FreeeAuthError: その他の認証エラー
    """
    data = _load_raw()
    expires_at = _parse_expires_at(data["expires_at"])
    if _is_expired_or_near(expires_at):
        logger.info(
            "freee_auth.token_near_expiry",
            expires_at=data["expires_at"],
            margin_sec=REFRESH_MARGIN_SEC,
        )
        data = force_refresh()
    return data["access_token"]


def status() -> dict[str, Any]:
    """CLI 表示用に現在のトークン状態をマスク付きで返す。"""
    try:
        data = _load_raw()
    except FreeeBootstrapRequiredError as e:
        return {"bootstrapped": False, "reason": str(e), "path": str(_token_file_path())}
    expires_at = _parse_expires_at(data["expires_at"])
    remaining = expires_at - _now_utc()
    return {
        "bootstrapped": True,
        "path": str(_token_file_path()),
        "access_token_masked": _mask(data["access_token"]),
        "refresh_token_masked": _mask(data["refresh_token"]),
        "obtained_at": data.get("obtained_at"),
        "expires_at": data["expires_at"],
        "expires_in_seconds": int(remaining.total_seconds()),
        "expires_in_minutes": int(remaining.total_seconds() // 60),
        "needs_refresh": _is_expired_or_near(expires_at),
        "company_id": data.get("company_id", ""),
    }


def _mask(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 8:
        return "*" * len(token)
    return f"{token[:4]}...{token[-4:]} (len={len(token)})"


def build_authorize_url() -> str:
    """再認可フロー用の URL を組み立てる（CLI / 通知メールから案内する用途）。"""
    from urllib.parse import urlencode

    params = {
        "client_id": settings.freee_client_id or "__FREEE_CLIENT_ID__",
        "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
        "response_type": "code",
        "prompt": "select_company",
    }
    return f"{FREEE_AUTHORIZE_BASE}?{urlencode(params)}"
