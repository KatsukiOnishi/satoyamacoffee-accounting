"""PDF・本文から `ExtractedInvoice` を構造化抽出する。

Anthropic API の document（PDF直接渡し）+ tools structured output を使う。
本文のみのメールは text + tools で同じスキーマに落とす。

`accounting.connectors.anthropic_vision.VisionExtractor` は画像専用なので、
本タスクは PDF/text を扱う薄いヘルパーをここに置く。既存タスクには触れない。
"""
from __future__ import annotations

import base64
from pathlib import Path

from anthropic import Anthropic

from accounting.config import settings
from accounting.core.logger import get_logger
from accounting.tasks.vendor_invoice.models import ExtractedInvoice

logger = get_logger("vendor_invoice.extractor")

DEFAULT_MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """あなたは日本の事業者間取引の請求書PDFを読み取る経理アシスタントです。
与えられた請求書から以下のフィールドを抽出し、必ず extract_invoice ツールを呼んで返してください。

- partner_name: 請求書の発行元事業者名（例: 株式会社ピーエスアイ、宮﨑会計事務所）
- issue_date: 請求書発行日 YYYY-MM-DD（不明なら null）
- due_date: 支払期日 YYYY-MM-DD（不明なら null）
- total_amount: 税込合計金額（整数円、カンマや円記号は除く）
- tax_amount: 消費税額（整数円、抜けてれば null）
- bank_account_info: 振込先口座の全文（例: "秋田銀行 大町支店 普通 1234567 カ）アキタサトヤマデザイン"）
- has_bank_account_info: 振込先口座が明記されていれば true。クレカ決済で口座記載なし、もしくは引落予告なら false
- line_items_summary: 主な明細1〜3行の要約（"WEB制作費 1式 200,000円" など）
- is_invoice: これが請求書（請求書 / ご請求 / Invoice）であれば true。領収書・見積書・契約書なら false
- confidence_notes: 抽出が不確実な点があれば日本語で簡潔に書く

判断の指針:
- "クレジットカードでお支払い"、"自動引落のご案内"、"領収書" などの文言があり振込先記載がないなら has_bank_account_info=false
- 領収書（領収しました）は is_invoice=false
"""


def _tool_def() -> dict:
    return {
        "name": "extract_invoice",
        "description": "請求書PDFから構造化データを抽出する",
        "input_schema": ExtractedInvoice.model_json_schema(),
    }


def _client() -> Anthropic:
    if not settings.anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY が未設定です（.env に追加してください）")
    return Anthropic(api_key=settings.anthropic_api_key)


def extract_from_pdf(pdf_path: Path, model: str = DEFAULT_MODEL) -> ExtractedInvoice:
    """PDF を Anthropic API の document content として渡して抽出する。"""
    data = pdf_path.read_bytes()
    if not data:
        raise ValueError(f"PDFファイルが空です: {pdf_path}")
    b64 = base64.standard_b64encode(data).decode()

    client = _client()
    resp = client.messages.create(
        model=model,
        max_tokens=2048,
        tools=[_tool_def()],
        tool_choice={"type": "tool", "name": "extract_invoice"},
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": "このPDFを extract_invoice ツールで抽出してください。",
                    },
                ],
            }
        ],
    )

    for block in resp.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == "extract_invoice"
        ):
            logger.info(
                "extractor.pdf_extracted",
                path=str(pdf_path),
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
            )
            return ExtractedInvoice.model_validate(block.input)
    raise RuntimeError(
        f"Anthropic API が tool_use ブロックを返しませんでした: {pdf_path}"
    )


def extract_from_body(
    subject: str, body_text: str, sender: str, model: str = DEFAULT_MODEL
) -> ExtractedInvoice:
    """添付PDFがない場合に、メール本文から請求書情報を抽出する。"""
    client = _client()
    user_text = (
        f"以下はベンダーから届いたメールです。請求書相当の内容を ExtractedInvoice として抽出してください。\n\n"
        f"From: {sender}\n"
        f"Subject: {subject}\n\n"
        f"--- 本文 ここから ---\n{body_text}\n--- 本文 ここまで ---"
    )
    resp = client.messages.create(
        model=model,
        max_tokens=2048,
        tools=[_tool_def()],
        tool_choice={"type": "tool", "name": "extract_invoice"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_text}],
    )
    for block in resp.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == "extract_invoice"
        ):
            logger.info(
                "extractor.body_extracted",
                sender=sender,
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
            )
            return ExtractedInvoice.model_validate(block.input)
    raise RuntimeError("Anthropic API が tool_use ブロックを返しませんでした（本文抽出）")
