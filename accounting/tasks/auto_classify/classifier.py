"""Claude による wallet_txn 分類ロジック。

仕様書 §5-3 のスキーマ・プロンプト方針を実装する。
"""
from __future__ import annotations

import json
from typing import Any

from accounting.connectors.anthropic_classifier import TextClassifier
from accounting.core.logger import get_logger
from accounting.tasks.auto_classify.models import (
    ClassificationResult,
    ClassifyWalletTxnOutput,
    WalletTxnForClassify,
)

log = get_logger("auto_classify.classifier")


_SYSTEM_PROMPT_TEMPLATE = """\
あなたは合同会社秋田里山デザイン（さとやまコーヒー）の経理担当アシスタントです。
freee 上の口座明細 1 件を見て、最も妥当な勘定科目・税区分・取引先・信頼度を判定してください。

【重要なルール】
- 出力は必ず classify_wallet_txn ツール経由で行うこと。
- account_item_name は **下記の freee 勘定科目マスタに存在する名前** のみ。新規作成しない。
- partner_name は **下記の freee 取引先マスタに存在する名前** のみ。該当なしなら null。
- tax_code_name は freee の税区分名（スペースなしの形式）。例:
    - 課税仕入  → "課対仕入10%"  (10%) / "課対仕入軽減8%" (8%)
    - 課税売上  → "課税売上10%"  (10%) / "課税売上軽減8%" (8%)
    - 銀行利息 → "非課売上"
    - 借入返済 → "対象外"
- confidence は 0.0-1.0:
    - 0.9 以上: 過去同パターンが多数 or 摘要が極めて特徴的
    - 0.7-0.9: 過去パターンあり、概ね確信
    - 0.5-0.7: 推測の域、人間確認推奨
    - 0.5 未満: 判定困難、スキップ推奨
- reason は 1-2 文。摘要中のどのキーワード・どの過去サンプルを根拠にしたか明示。

【freee 勘定科目マスタ（抜粋）】
{account_items_text}

【freee 取引先マスタ（抜粋）】
{partners_text}

【過去 90 日の取引サンプル】
{examples_text}
"""


_USER_PROMPT_TEMPLATE = """\
以下の wallet_txn 1 件について勘定科目を判定してください。

wallet_txn_description: {description}
wallet_txn_amount: {amount}  (符号: {sign})
walletable_name: {walletable_name}
transaction_date: {date}

必ず classify_wallet_txn ツールを呼んで構造化結果を返してください。
"""


def _format_master_text(items: list[dict[str, Any]], limit: int = 80) -> str:
    """マスタ一覧をプロンプトに乗せやすい形に整形（先頭 limit 件）。"""
    lines = []
    for it in items[:limit]:
        name = (it.get("name") or "").strip()
        if not name:
            continue
        lines.append(f"- {name}")
    return "\n".join(lines)


def _format_examples_text(examples: list[dict[str, Any]], limit: int = 10) -> str:
    """few-shot 用サンプルを整形（仕様書 §13-Q4: 最大 10 件）。"""
    if not examples:
        return "(過去サンプルなし)"
    lines = []
    for ex in examples[:limit]:
        lines.append(
            f"- type={ex.get('type')} date={ex.get('date')} "
            f"partner={ex.get('partner_name') or '(なし)'} "
            f"amount={ex.get('amount')} "
            f"desc={(ex.get('description') or '')[:30]!r} "
            f"→ {ex.get('account_item_name') or ''} / {ex.get('tax_name') or ''}"
        )
    return "\n".join(lines)


def build_prompts(
    txn: WalletTxnForClassify,
    *,
    account_items: list[dict[str, Any]],
    partners: list[dict[str, Any]],
    past_examples: list[dict[str, Any]],
) -> tuple[str, str]:
    """(system_prompt, user_prompt) を返す。"""
    system = _SYSTEM_PROMPT_TEMPLATE.format(
        account_items_text=_format_master_text(account_items),
        partners_text=_format_master_text(partners, limit=60),
        examples_text=_format_examples_text(past_examples),
    )
    user = _USER_PROMPT_TEMPLATE.format(
        description=txn.description,
        amount=txn.amount,
        sign="入金(+)" if txn.amount > 0 else "出金(-)",
        walletable_name=txn.walletable_name or "(不明)",
        date=txn.date.isoformat(),
    )
    return system, user


def classify_one(
    txn: WalletTxnForClassify,
    *,
    classifier: TextClassifier,
    account_items: list[dict[str, Any]],
    partners: list[dict[str, Any]],
    past_examples: list[dict[str, Any]],
) -> ClassifyWalletTxnOutput:
    """1件 wallet_txn を Claude に投げて分類結果を返す。"""
    system, user = build_prompts(
        txn,
        account_items=account_items,
        partners=partners,
        past_examples=past_examples,
    )
    out = classifier.classify(
        user_prompt=user,
        schema=ClassifyWalletTxnOutput,
        system_prompt=system,
    )
    log.info(
        "auto_classify.classified",
        wallet_txn_id=txn.id,
        account_item=out.account_item_name,
        confidence=out.confidence,
    )
    return out


def resolve_masters(
    output: ClassifyWalletTxnOutput,
    masters: dict[str, Any],
) -> tuple[int | None, int | None, int | None]:
    """Claude の出力（名前文字列）を freee マスタ ID に解決する。

    Returns:
      (account_item_id, tax_code_id, partner_id) のタプル。
      解決できなければそのフィールドは None。
    """
    from accounting.tasks.ar_reconcile.matcher import normalize_partner_name

    ai = masters.get("account_items_by_name", {}).get(output.account_item_name)
    account_item_id = int(ai["id"]) if ai and ai.get("id") is not None else None

    tx = masters.get("tax_codes_by_name", {}).get(output.tax_code_name)
    tax_code_id = int(tx["code"]) if tx and tx.get("code") is not None else None

    partner_id: int | None = None
    if output.partner_name:
        key = normalize_partner_name(output.partner_name)
        if key:
            p = masters.get("partners_by_norm_name", {}).get(key)
            if p and p.get("id") is not None:
                partner_id = int(p["id"])
    return account_item_id, tax_code_id, partner_id


def serialize_alternative(output: ClassifyWalletTxnOutput) -> str | None:
    if output.alternative is None:
        return None
    return json.dumps(
        output.alternative.model_dump(), ensure_ascii=False, default=str
    )
