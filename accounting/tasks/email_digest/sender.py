"""Resend 経由のダイジェストメール送信。

`accounting.core.notifier._send` はテキスト本文用なので、HTML 送信用に薄い関数を
追加する。Resend SDK の `Emails.send` は `text` と `html` の両方を受け付ける。
"""
from __future__ import annotations

from typing import Any

from accounting.config import settings
from accounting.core.logger import get_logger

logger = get_logger("email_digest.sender")


class DigestSendResult:
    def __init__(
        self,
        *,
        success: bool,
        recipient: str,
        subject: str,
        resend_message_id: str | None = None,
        error: str | None = None,
    ) -> None:
        self.success = success
        self.recipient = recipient
        self.subject = subject
        self.resend_message_id = resend_message_id
        self.error = error


def send_digest(
    *,
    subject: str,
    html: str,
    text_fallback: str | None = None,
    dry_run: bool = False,
) -> DigestSendResult:
    """HTML 本文を Resend で送信する。

    dry_run=True なら API を叩かず、success=True で疑似的に返す（テスト・標準出力用）。
    """
    recipient = settings.notify_email
    sender = settings.from_email

    if dry_run:
        logger.info(
            "email_digest.dry_run",
            subject=subject,
            recipient=recipient,
            html_length=len(html),
        )
        return DigestSendResult(
            success=True,
            recipient=recipient,
            subject=subject,
        )

    if not settings.resend_api_key:
        logger.warning(
            "email_digest.skip",
            reason="RESEND_API_KEY not set",
            subject=subject,
        )
        return DigestSendResult(
            success=False,
            recipient=recipient,
            subject=subject,
            error="RESEND_API_KEY not set",
        )
    if not recipient or not sender:
        logger.warning(
            "email_digest.skip",
            reason="NOTIFY_EMAIL or FROM_EMAIL not set",
            subject=subject,
        )
        return DigestSendResult(
            success=False,
            recipient=recipient,
            subject=subject,
            error="NOTIFY_EMAIL or FROM_EMAIL not set",
        )

    import resend

    resend.api_key = settings.resend_api_key
    payload: dict[str, Any] = {
        "from": sender,
        "to": [recipient],
        "subject": subject,
        "html": html,
    }
    if text_fallback:
        payload["text"] = text_fallback
    try:
        resp = resend.Emails.send(payload)
        message_id = (resp or {}).get("id") if isinstance(resp, dict) else None
        logger.info(
            "email_digest.sent",
            subject=subject,
            recipient=recipient,
            message_id=message_id,
        )
        return DigestSendResult(
            success=True,
            recipient=recipient,
            subject=subject,
            resend_message_id=message_id,
        )
    except Exception as e:
        logger.error(
            "email_digest.send_failed",
            subject=subject,
            recipient=recipient,
            error=str(e),
        )
        return DigestSendResult(
            success=False,
            recipient=recipient,
            subject=subject,
            error=str(e),
        )
