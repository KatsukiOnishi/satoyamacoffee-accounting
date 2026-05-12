"""トークン認証ミドルウェア。

サーバ起動時に生成されたトークンを `app.state.auth_token` に格納し、
クッキー or クエリ `?token=` で照合する。両方無ければ 401 HTML を返す。
"""
from __future__ import annotations

import secrets

from fastapi import Request
from fastapi.responses import HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

COOKIE_NAME = "accounting_token"
COOKIE_MAX_AGE = 86400  # 24時間


def generate_token() -> str:
    return secrets.token_urlsafe(24)


_UNAUTHORIZED_HTML = """\
<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><title>認証エラー</title>
<script src="https://cdn.tailwindcss.com"></script>
</head><body class="bg-stone-50">
  <main class="min-h-screen flex items-center justify-center p-6">
    <div class="max-w-md w-full bg-white rounded-2xl shadow p-8 border border-stone-200">
      <h1 class="text-2xl font-bold text-stone-800 mb-3">認証が必要です</h1>
      <p class="text-stone-600 leading-relaxed">
        このページを開くには、サーバ起動時にターミナルに表示された URL
        （<code class="bg-stone-100 px-1 rounded">?token=...</code> 付き）を使ってください。
      </p>
      <p class="text-stone-500 text-sm mt-4">
        トークンはプロセスごとに毎回ランダムに発行され、再起動すると変わります。
      </p>
    </div>
  </main>
</body></html>
"""


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """`/static/*` 以外のすべてのリクエストにトークン認証を要求する。"""

    EXEMPT_PREFIXES = ("/static",)

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if any(path.startswith(p) for p in self.EXEMPT_PREFIXES):
            return await call_next(request)

        expected = request.app.state.auth_token
        cookie = request.cookies.get(COOKIE_NAME)
        if cookie and secrets.compare_digest(cookie, expected):
            return await call_next(request)

        query = request.query_params.get("token")
        if query and secrets.compare_digest(query, expected):
            response = await call_next(request)
            response.set_cookie(
                COOKIE_NAME,
                expected,
                httponly=True,
                samesite="lax",
                max_age=COOKIE_MAX_AGE,
            )
            return response

        return HTMLResponse(content=_UNAUTHORIZED_HTML, status_code=401)
