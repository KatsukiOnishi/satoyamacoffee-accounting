"""Gmail から候補メールを取得して、添付をローカルに保存する。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from accounting.config import settings
from accounting.connectors.gmail import (
    GmailAttachment,
    GmailClient,
    GmailMessage,
    build_after_query,
)
from accounting.core.logger import get_logger

logger = get_logger("vendor_invoice.fetcher")


def _safe_filename(name: str) -> str:
    # Path separator / 制御文字を弾く
    bad = "/\\:*?\"<>|\r\n\t"
    return "".join("_" if c in bad else c for c in name)[:160] or "attachment"


def attachment_save_dir(received_at: datetime) -> Path:
    base = Path(settings.vendor_invoice_attachments_dir).expanduser()
    return base / received_at.strftime("%Y-%m-%d")


def attachment_save_path(
    message: GmailMessage, attachment: GmailAttachment
) -> Path:
    d = attachment_save_dir(message.received_at)
    fname = f"{message.message_id}__{_safe_filename(attachment.filename)}"
    return d / fname


def fetch_recent_messages(days: int, max_results: int = 500) -> list[GmailMessage]:
    """過去 N 日分のメッセージを取得（INBOX のみ、件数上限つき）。"""
    client = GmailClient()
    query = build_after_query(days)
    logger.info("vendor_invoice.fetcher.search", query=query, days=days)
    ids = client.search_messages(query, max_results=max_results)
    messages: list[GmailMessage] = []
    for mid in ids:
        try:
            messages.append(client.get_message(mid))
        except Exception as e:
            logger.error(
                "vendor_invoice.fetcher.get_message_failed",
                message_id=mid,
                error=str(e),
            )
    return messages


def download_attachment_if_pdf_or_zip(
    message: GmailMessage,
    attachment: GmailAttachment,
) -> Path | None:
    """PDF/ZIPだけダウンロードする。他形式は無視。"""
    name_lower = attachment.filename.lower()
    is_pdf = name_lower.endswith(".pdf") or attachment.mime_type == "application/pdf"
    is_zip = (
        name_lower.endswith(".zip")
        or attachment.mime_type == "application/zip"
        or attachment.mime_type == "application/x-zip-compressed"
    )
    if not (is_pdf or is_zip):
        return None
    save_to = attachment_save_path(message, attachment)
    client = GmailClient()
    return client.download_attachment(message.message_id, attachment.attachment_id, save_to)
