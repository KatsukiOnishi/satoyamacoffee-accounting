"""Gmail API クライアント。

vendor-invoice タスクから利用される薄いラッパー。
- メッセージ検索（query / after: フィルタ）
- メッセージ取得（headers / body / 添付一覧）
- 添付ダウンロード

認証は `accounting.core.gmail_auth.get_credentials()` 経由。
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional

from accounting.config import settings
from accounting.core.gmail_auth import get_credentials
from accounting.core.logger import get_logger

logger = get_logger("gmail")


@dataclass
class GmailAttachment:
    """添付ファイルのメタ情報。"""

    attachment_id: str
    filename: str
    mime_type: str
    size_bytes: int


@dataclass
class GmailMessage:
    """1メッセージの正規化表現。"""

    message_id: str
    thread_id: str
    sender: str  # 「Name <addr@example.com>」を addr@example.com だけに切り詰めたもの
    sender_raw: str  # ヘッダ生値（表示名込み）
    subject: str
    received_at: datetime
    snippet: str
    body_text: str  # text/plain + text/html を flatten した雑な本文
    attachments: list[GmailAttachment]


def _build_service() -> Any:
    """googleapiclient.discovery.Resource を返す。"""
    from googleapiclient.discovery import build

    creds = get_credentials()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _extract_addr(raw: str) -> str:
    if not raw:
        return ""
    parsed = getaddresses([raw])
    if parsed and parsed[0][1]:
        return parsed[0][1].lower()
    return raw.lower()


def _decode_b64url(data: str) -> bytes:
    # Gmail API は URL-safe base64（パディング無し）を返す
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _walk_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """payload を再帰的に flatten して全 part を返す。"""
    parts: list[dict[str, Any]] = [payload]
    queue = [payload]
    while queue:
        cur = queue.pop()
        for child in cur.get("parts", []) or []:
            parts.append(child)
            queue.append(child)
    return parts


def _extract_body_and_attachments(
    payload: dict[str, Any],
) -> tuple[str, list[GmailAttachment]]:
    """text/plain を優先して body を取り、添付メタ情報を集める。"""
    text_chunks: list[str] = []
    html_chunks: list[str] = []
    attachments: list[GmailAttachment] = []

    for part in _walk_parts(payload):
        mime = part.get("mimeType", "")
        body = part.get("body", {}) or {}
        filename = part.get("filename", "") or ""

        if filename and body.get("attachmentId"):
            attachments.append(
                GmailAttachment(
                    attachment_id=body["attachmentId"],
                    filename=filename,
                    mime_type=mime,
                    size_bytes=int(body.get("size") or 0),
                )
            )
            continue

        data = body.get("data")
        if not data:
            continue
        try:
            decoded = _decode_b64url(data).decode("utf-8", errors="replace")
        except Exception:
            continue
        if mime == "text/plain":
            text_chunks.append(decoded)
        elif mime == "text/html":
            html_chunks.append(decoded)

    body_text = "\n".join(text_chunks).strip()
    if not body_text and html_chunks:
        # フォールバック: HTML タグを雑に剥がす（pip依存を避ける）
        import re

        raw = "\n".join(html_chunks)
        body_text = re.sub(r"<[^>]+>", " ", raw)
        body_text = re.sub(r"\s+", " ", body_text).strip()
    return body_text, attachments


def _header(headers: list[dict[str, str]], name: str) -> str:
    name_lower = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")
    return ""


class GmailClient:
    """Gmail API の薄いラッパー。"""

    def __init__(self, user_id: Optional[str] = None) -> None:
        self.user_id = user_id or settings.gmail_user_id or "me"
        self._service: Any = None

    @property
    def service(self) -> Any:
        if self._service is None:
            self._service = _build_service()
        return self._service

    def search_messages(self, query: str, max_results: int = 500) -> list[str]:
        """query にマッチするメッセージIDを返す（自動ページング）。

        Args:
            query: Gmail 検索クエリ（例: `after:2026/04/14 (request OR invoice)`）
            max_results: 上限件数

        Returns: message_id のリスト（受信日時降順）
        """
        ids: list[str] = []
        page_token: Optional[str] = None
        while len(ids) < max_results:
            resp = (
                self.service.users()
                .messages()
                .list(
                    userId=self.user_id,
                    q=query,
                    pageToken=page_token,
                    maxResults=min(500, max_results - len(ids)),
                )
                .execute()
            )
            for m in resp.get("messages", []) or []:
                ids.append(m["id"])
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        logger.info("gmail.search_messages", query=query, total=len(ids))
        return ids

    def get_message(self, message_id: str) -> GmailMessage:
        msg = (
            self.service.users()
            .messages()
            .get(userId=self.user_id, id=message_id, format="full")
            .execute()
        )
        payload = msg.get("payload", {}) or {}
        headers = payload.get("headers", []) or []
        subject = _header(headers, "Subject")
        sender_raw = _header(headers, "From")
        sender = _extract_addr(sender_raw)
        date_str = _header(headers, "Date")
        try:
            received_at = parsedate_to_datetime(date_str)
            if received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=timezone.utc)
        except Exception:
            received_at = datetime.now(timezone.utc)

        body_text, attachments = _extract_body_and_attachments(payload)
        return GmailMessage(
            message_id=msg["id"],
            thread_id=msg.get("threadId", ""),
            sender=sender,
            sender_raw=sender_raw,
            subject=subject,
            received_at=received_at,
            snippet=msg.get("snippet", "") or "",
            body_text=body_text,
            attachments=attachments,
        )

    def download_attachment(
        self,
        message_id: str,
        attachment_id: str,
        save_to: Path,
    ) -> Path:
        """添付バイト列をダウンロードしてファイルに保存する。"""
        att = (
            self.service.users()
            .messages()
            .attachments()
            .get(userId=self.user_id, messageId=message_id, id=attachment_id)
            .execute()
        )
        data = att.get("data", "")
        raw = _decode_b64url(data)
        save_to.parent.mkdir(parents=True, exist_ok=True)
        save_to.write_bytes(raw)
        logger.info(
            "gmail.attachment_downloaded",
            message_id=message_id,
            attachment_id=attachment_id,
            path=str(save_to),
            size_bytes=len(raw),
        )
        return save_to


def build_after_query(days: int, extra_terms: Optional[str] = None) -> str:
    """`accounting vendor-invoice scan --days N` 用の Gmail クエリを組む。

    INBOX に絞り、未送信・下書き・ゴミ箱は除外する。
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y/%m/%d")
    base = f"after:{cutoff} in:inbox -in:sent -in:draft -in:trash"
    if extra_terms:
        return f"{base} ({extra_terms})"
    # 添付ありメール OR 件名/本文に請求書系のキーワードを含むメール
    return (
        f"{base} (has:attachment OR \"請求書\" OR \"ご請求\" OR invoice OR Invoice OR \"領収\")"
    )
