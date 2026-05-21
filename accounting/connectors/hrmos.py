"""HRMOS（勤怠）コネクタ。

責務: HRMOS にログインして、指定月の社員ごとの勤怠 CSV を取得する。

スコープ:
- ログイン（Rails の authenticity_token CSRF を抽出して form POST）
- 指定月の user_id 一覧スクレイプ（/bulk_approvals?date=YYYY-MM の HTML から抽出）
- 社員別 CSV ダウンロード（/works/csv_download?date=YYYY-MM&user_id=ID、Shift-JIS bytes をそのまま返す）

HRMOS フリープランの制約上、1年以上前のデータは取れない（月末20日に前月分を取りに行く運用なら影響なし）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from accounting.config import settings
from accounting.core.logger import get_logger

logger = get_logger("hrmos")

_USER_ID_PATTERN = re.compile(r"/approvals/(\d+)/")
# /staffs ページの社員リンク: <a href="/staffs/{id}">{name}</a>
# 「コピー登録」リンク (/staffs/{id}/copy) は除外したいので末尾が引用符のものだけマッチ
_STAFF_LINK_PATTERN = re.compile(r'<a href="/staffs/(\d+)">([^<]+)</a>')
_CSRF_PATTERN = re.compile(
    r'<input[^>]+name=["\']authenticity_token["\'][^>]+value=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HrmosCsv:
    """1社員1月分の勤怠 CSV（Shift-JIS bytes をそのまま保持）。"""

    user_id: int
    yyyymm: str
    filename: str
    content: bytes


@dataclass(frozen=True)
class HrmosStaff:
    """HRMOS の /staffs ページから抽出した1名の社員情報。"""

    user_id: int
    name: str


class HrmosClient:
    """HRMOS スクレイピングクライアント。

    HRMOS は公開 API がないため、ログイン画面の CSRF token を抽出して form POST し、
    Cookie セッションで /bulk_approvals や /works/csv_download を叩く。
    タスク先頭で `login()` を必ず呼ぶ（セッション寿命が不明なため使い回しを避ける）。
    """

    def __init__(
        self,
        login_url: str | None = None,
        login_id: str | None = None,
        password: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.login_url = login_url or settings.hrmos_login_url
        self.login_id = login_id or settings.hrmos_user
        self.password = password or settings.hrmos_pass
        if not self.login_url:
            raise ValueError("HRMOS_LOGIN_URL が未設定です")
        if not self.login_id or not self.password:
            raise ValueError("HRMOS_USER / HRMOS_PASS が未設定です")
        parsed = urlparse(self.login_url)
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        # follow_redirects=True で /home などへのリダイレクト後の Set-Cookie も拾う
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "satoyamacoffee-accounting/sync-hrmos"},
        )
        self._logged_in = False

    def __enter__(self) -> "HrmosClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def login(self) -> None:
        """CSRF トークンを抽出し、form POST でセッションを確立する。"""
        resp = self._client.get(self.login_url)
        resp.raise_for_status()
        match = _CSRF_PATTERN.search(resp.text)
        if not match:
            raise RuntimeError(
                "ログイン画面から authenticity_token を抽出できませんでした"
                "（HRMOS のログイン画面構造が変わった可能性）"
            )
        token = match.group(1)

        post_resp = self._client.post(
            self.login_url,
            data={
                "utf8": "✓",
                "authenticity_token": token,
                "user[login_id]": self.login_id,
                "user[password]": self.password,
            },
            headers={"Referer": self.login_url},
        )
        post_resp.raise_for_status()
        # ログイン失敗時はログイン画面に戻ってくる（authenticity_token input が再出現）
        if _CSRF_PATTERN.search(post_resp.text) and "login_id" in post_resp.text:
            raise RuntimeError(
                "HRMOS ログインに失敗しました（資格情報が間違っている可能性）"
            )
        self._logged_in = True
        logger.info("hrmos_login_success", login_id=self.login_id)

    def list_user_ids_for_month(self, yyyymm: str) -> list[int]:
        """指定月の /bulk_approvals ページから user_id を抽出して返す。

        引数 yyyymm は `YYYY-MM` 形式。レスポンス HTML 中の `/approvals/{user_id}/...` リンクから
        user_id を集合化（昇順）して返す。該当月に勤怠が1日もない社員は含まれない（HRMOS仕様）。

        ※ sync-hrmos タスクのデフォルトでは `list_active_staffs()` を使う方針。本メソッドは
        「その月に承認待ち or 承認済みの社員だけ」を対象にしたい場合の補助。
        """
        self._ensure_logged_in()
        url = f"{self.origin}/bulk_approvals"
        resp = self._client.get(url, params={"date": yyyymm})
        resp.raise_for_status()
        ids = sorted({int(m) for m in _USER_ID_PATTERN.findall(resp.text)})
        logger.info("hrmos_user_ids", yyyymm=yyyymm, count=len(ids), ids=ids)
        return ids

    def list_active_staffs(self) -> list[HrmosStaff]:
        """`/staffs` ページから在籍中の全社員を抽出して返す（user_id 昇順）。

        HTML は `<a href="/staffs/{id}">{name}</a>` 構造。`/staffs/{id}/copy` 等の
        派生リンクはマッチしないようにしてある（パターン末尾の `>` で確実に終端）。
        勤怠ゼロの月でも全員返るのが /bulk_approvals との違い。
        """
        self._ensure_logged_in()
        url = f"{self.origin}/staffs"
        resp = self._client.get(url)
        resp.raise_for_status()
        seen: set[int] = set()
        staffs: list[HrmosStaff] = []
        for m in _STAFF_LINK_PATTERN.finditer(resp.text):
            uid = int(m.group(1))
            if uid in seen:
                continue
            seen.add(uid)
            staffs.append(HrmosStaff(user_id=uid, name=m.group(2).strip()))
        staffs.sort(key=lambda s: s.user_id)
        logger.info(
            "hrmos_active_staffs",
            count=len(staffs),
            ids=[s.user_id for s in staffs],
        )
        return staffs

    def download_csv(self, yyyymm: str, user_id: int) -> HrmosCsv:
        """1社員1月分の勤怠 CSV を取得する（Shift-JIS bytes をそのまま返す）。"""
        self._ensure_logged_in()
        url = f"{self.origin}/works/csv_download"
        resp = self._client.get(url, params={"date": yyyymm, "user_id": user_id})
        resp.raise_for_status()
        if not resp.content:
            raise RuntimeError(
                f"HRMOS CSV が空 (yyyymm={yyyymm}, user_id={user_id})。"
                "対象月のデータが存在しない可能性"
            )
        filename = f"hrmos_{yyyymm}_{user_id}.csv"
        logger.info(
            "hrmos_csv_downloaded",
            yyyymm=yyyymm,
            user_id=user_id,
            bytes=len(resp.content),
        )
        return HrmosCsv(
            user_id=user_id,
            yyyymm=yyyymm,
            filename=filename,
            content=resp.content,
        )

    def _ensure_logged_in(self) -> None:
        if not self._logged_in:
            raise RuntimeError("login() を先に呼んでください")
