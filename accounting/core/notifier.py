from __future__ import annotations

import traceback
from typing import Any

from accounting.config import settings
from accounting.core.logger import get_logger

logger = get_logger("notifier")


def _send(subject: str, body: str) -> None:
    if not settings.resend_api_key:
        logger.warning("notifier.skip", reason="RESEND_API_KEY not set", subject=subject)
        return
    if not settings.notify_email or not settings.from_email:
        logger.warning(
            "notifier.skip",
            reason="NOTIFY_EMAIL or FROM_EMAIL not set",
            subject=subject,
        )
        return

    import resend

    resend.api_key = settings.resend_api_key
    try:
        resend.Emails.send(
            {
                "from": settings.from_email,
                "to": [settings.notify_email],
                "subject": subject,
                "text": body,
            }
        )
        logger.info("notifier.sent", subject=subject)
    except Exception as e:
        logger.error("notifier.failed", subject=subject, error=str(e))


def notify_failure(task: str, run_id: str, error: Exception, context: dict[str, Any]) -> None:
    subject = f"[satoyamacoffee-accounting] {task} failed at {run_id}"
    tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    body_lines = [
        f"Task: {task}",
        f"Run ID: {run_id}",
        f"Error: {type(error).__name__}: {error}",
        "",
        "Context:",
    ]
    for k, v in context.items():
        body_lines.append(f"  {k}: {v}")
    body_lines += ["", "Traceback:", tb]
    _send(subject, "\n".join(body_lines))


def notify_refresh_token_invalid(reauth_url: str) -> None:
    """freee の refresh_token が無効化された時の再認可案内メール。

    呼び出される代表シナリオ:
      - 90日間 refresh されず失効
      - 競合で他プロセスが先に消費した（refresh_token は 1 回限り使用可能）
      - ユーザー側で freee の権限を取り消した

    通知後、人手で `accounting auth init` または再認可フロー実行が必要。
    """
    subject = "[satoyamacoffee-accounting] freee refresh_token が無効化されました"
    body = (
        "freee API の refresh_token が無効化されました。\n"
        "月次決算ハブはトークン更新に失敗しています。\n"
        "\n"
        "対応:\n"
        "1. 下記URLをブラウザで開いて再認可\n"
        f"   {reauth_url}\n"
        "2. 認可コードを取得 → curl で access_token + refresh_token を取得\n"
        "3. `accounting auth init --force --access-token X --refresh-token Y` で再投入\n"
        "\n"
        "詳細手順はリポジトリの CLAUDE.md（freee API 連携 セクション）を参照。\n"
    )
    _send(subject, body)


def notify_summary(task: str, run_id: str, summary: dict[str, Any]) -> None:
    if not settings.notify_on_success:
        logger.debug("notifier.summary_skipped", reason="NOTIFY_ON_SUCCESS=false")
        return
    subject = f"[satoyamacoffee-accounting] {task} summary {run_id}"
    body_lines = [f"Task: {task}", f"Run ID: {run_id}", "", "Summary:"]
    for k, v in summary.items():
        body_lines.append(f"  {k}: {v}")
    _send(subject, "\n".join(body_lines))
