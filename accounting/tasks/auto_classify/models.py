"""auto-classify タスクのデータモデル（Pydantic）。"""
from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field


class WalletTxnForClassify(BaseModel):
    """分類対象の wallet_txn の必要フィールド。"""

    id: int
    date: date
    description: str
    amount: int                  # 符号付き（負=出金、正=入金）
    walletable_type: str | None = None
    walletable_id: int | None = None
    walletable_name: str | None = None
    entry_side: str | None = None


class ClassifyWalletTxnInput(BaseModel):
    """Anthropic に渡す入力スキーマ（参考用、システムプロンプトで参照）。"""

    wallet_txn_description: str
    wallet_txn_amount: int
    walletable_name: str
    transaction_date: str


class ClassifyAlternative(BaseModel):
    """第2候補。"""

    account_item_name: str
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


class ClassifyWalletTxnOutput(BaseModel):
    """Anthropic から受け取る tool_use の input スキーマ。"""

    account_item_name: str = Field(
        description="freee マスタの勘定科目名。例: '荷造運賃' '消耗品費'"
    )
    tax_code_name: str = Field(
        description=(
            "freee マスタの税区分名。スペース無し形式。"
            "例: '課対仕入10%' '課税売上10%' '対象外'"
        )
    )
    partner_name: str | None = Field(
        default=None,
        description="取引先名（freee に存在するものに限る、なければ null）",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="0.0-1.0 の判定信頼度。仕様書 §5-3 のしきい値基準を参照",
    )
    reason: str = Field(description="1-2文の判定理由。摘要中のキーワードを引用すること")
    alternative: ClassifyAlternative | None = Field(
        default=None, description="第2候補（あれば）"
    )


class ClassificationResult(BaseModel):
    """1件の wallet_txn の処理結果。"""

    wallet_txn: WalletTxnForClassify
    output: ClassifyWalletTxnOutput | None = None
    resolved_account_item_id: int | None = None
    resolved_tax_code_id: int | None = None
    resolved_partner_id: int | None = None
    action_taken: str = "skipped"     # shadow_logged / registered / review_required / skipped / failed
    freee_deal_id: int | None = None
    error_message: str | None = None
    excluded_reason: str | None = None


class AutoClassifyRunResult(BaseModel):
    run_id: str
    mode: str
    total_fetched: int = 0
    total_excluded: int = 0
    shadow_logged: int = 0
    registered: int = 0
    review_required: int = 0
    skipped: int = 0
    failed: int = 0
