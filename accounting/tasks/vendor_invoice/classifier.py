"""送信者・件名・添付から「銀行振込ベンダー請求書か否か」を1次分類する。

Vision 抽出に進むかどうかをここで判定する。
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from accounting.connectors.gmail import GmailMessage
from accounting.tasks.vendor_invoice.blacklists import (
    get_known_vendor,
    has_invoice_keyword,
    is_excluded_sender,
)
from accounting.tasks.vendor_invoice.models import ClassifierVerdict


def is_encrypted_zip(path: Path) -> bool:
    """ZIP が暗号化（パスワード保護）されているかを判定する。"""
    if not path.exists():
        return False
    try:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.flag_bits & 0x1:
                    return True
        return False
    except zipfile.BadZipFile:
        return False


def classify_initial(message: GmailMessage) -> ClassifierVerdict:
    """添付ファイルを開く前段階での1次分類。

    Vision 抽出に進むべきかどうかをここで決める。
    `bank_transfer_invoice` でも確定ではなく、後段の Vision 結果で has_bank_account_info=false
    なら再分類される。
    """
    sender = (message.sender or "").lower().strip()

    if not sender:
        return ClassifierVerdict(
            classification="needs_review",
            notes="sender_header_missing",
        )

    # KNOWN: 銀行振込ベンダーとして送信者一致 → 添付処理に進む
    known = get_known_vendor(sender)
    if known:
        return ClassifierVerdict(
            classification="bank_transfer_invoice",
            notes=f"known_vendor:{known.get('partner_name_hint', '')}",
        )

    # 1次除外
    if is_excluded_sender(sender):
        domain = sender.split("@", 1)[1] if "@" in sender else ""
        reason = "blacklisted_sender"
        from accounting.tasks.vendor_invoice.blacklists import EXCLUDED_DOMAINS

        if domain and domain in EXCLUDED_DOMAINS and sender not in get_excluded_set():
            reason = "blacklisted_domain"
        return ClassifierVerdict(
            classification="excluded",
            exclusion_reason=reason,  # type: ignore[arg-type]
            notes=f"sender={sender}",
        )

    # キーワード判定
    if not has_invoice_keyword(message.subject, message.snippet, message.body_text):
        return ClassifierVerdict(
            classification="excluded",
            exclusion_reason="no_invoice_keyword",
            notes=f"subject={message.subject[:60]}",
        )

    # 添付があるなら Vision 抽出に進む、ないなら本文抽出フェーズへ
    if message.attachments:
        return ClassifierVerdict(
            classification="bank_transfer_invoice",
            notes="has_attachments",
        )
    return ClassifierVerdict(
        classification="no_attachment",
        notes="body_only_message",
    )


def get_excluded_set() -> set[str]:
    """循環import避けのための薄いラッパー。"""
    from accounting.tasks.vendor_invoice.blacklists import EXCLUDED_SENDERS

    return EXCLUDED_SENDERS
