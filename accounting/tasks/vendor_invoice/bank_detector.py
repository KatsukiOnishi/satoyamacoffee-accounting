"""Vision 抽出結果から「銀行振込ベンダー請求書」かを最終判定する。

ロジックは単純:
1. is_invoice=false なら除外（領収書・見積書など）
2. has_bank_account_info=false なら除外（クレカ決済 / 引落系の可能性）
3. それ以外は bank_transfer_invoice 確定
"""
from __future__ import annotations

from accounting.tasks.vendor_invoice.models import ClassifierVerdict, ExtractedInvoice


def reclassify_from_extraction(extracted: ExtractedInvoice) -> ClassifierVerdict:
    if not extracted.is_invoice:
        return ClassifierVerdict(
            classification="excluded",
            exclusion_reason="not_an_invoice",
            notes=extracted.confidence_notes or "",
        )
    if not extracted.has_bank_account_info:
        return ClassifierVerdict(
            classification="excluded",
            exclusion_reason="no_bank_account_info",
            notes="probably credit_card_subscription",
        )
    return ClassifierVerdict(
        classification="bank_transfer_invoice",
        notes=extracted.confidence_notes or "",
    )
