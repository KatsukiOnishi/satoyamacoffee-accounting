"""vendor-invoice タスクの Pydantic モデル。"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


# Vision が抽出する請求書構造化データ
class ExtractedInvoice(BaseModel):
    partner_name: str = Field(description="発行元の事業者名（例: 株式会社ピーエスアイ）")
    issue_date: date | None = Field(default=None, description="請求書発行日 YYYY-MM-DD")
    due_date: date | None = Field(default=None, description="支払期日 YYYY-MM-DD")
    total_amount: int = Field(description="税込合計金額（整数円）")
    tax_amount: int | None = Field(default=None, description="消費税額（整数円、抜けてれば null）")
    bank_account_info: str | None = Field(
        default=None,
        description='振込先口座（"XX銀行 XX支店 普通 1234567 カナ名義"のように全文）',
    )
    has_bank_account_info: bool = Field(
        description="振込先口座が明記されているか（クレカ決済で口座記載なしなら false）"
    )
    line_items_summary: str = Field(
        default="",
        description='主な明細の要約（例: "WEB制作費 1式 200,000円"）',
    )
    is_invoice: bool = Field(description="これは請求書か（領収書・見積書なら false）")
    confidence_notes: str = Field(default="", description="抽出の不確実性メモ（人間レビュー用）")


Classification = Literal[
    "bank_transfer_invoice",
    "excluded",
    "needs_review",
    "encrypted_zip",
    "no_attachment",
]

ExclusionReason = Literal[
    "blacklisted_sender",
    "blacklisted_domain",
    "no_invoice_keyword",
    "no_bank_account_info",
    "not_an_invoice",
    "self_issued",
    "credit_card_subscription",
]


class ClassifierVerdict(BaseModel):
    """classifier の判定結果。"""

    classification: Classification
    exclusion_reason: ExclusionReason | None = None
    notes: str = ""
