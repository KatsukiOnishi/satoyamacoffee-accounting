"""Claude API テキスト分類ラッパー（auto-classify タスク用）。

anthropic_vision.py と同じ「tools structured output」パターンを踏襲する。
画像ではなく wallet_txn のテキスト情報を渡し、勘定科目・税区分・取引先・信頼度を返させる。
"""
from __future__ import annotations

from typing import Type, TypeVar

from anthropic import Anthropic
from pydantic import BaseModel

from accounting.config import settings
from accounting.core.logger import get_logger

T = TypeVar("T", bound=BaseModel)


class ClassifierError(Exception):
    pass


class TextClassifier:
    """wallet_txn 分類用の Anthropic API ラッパー。"""

    def __init__(self, model: str = "claude-sonnet-4-6") -> None:
        if not settings.anthropic_api_key:
            raise ClassifierError(
                "ANTHROPIC_API_KEY が未設定です（.env に追加してください）"
            )
        self.client = Anthropic(api_key=settings.anthropic_api_key)
        self.model = model
        self.log = get_logger("anthropic_classifier")

    def classify(
        self,
        *,
        user_prompt: str,
        schema: Type[T],
        system_prompt: str,
        tool_name: str = "classify_wallet_txn",
        max_tokens: int = 1024,
    ) -> T:
        """user_prompt を投入し、tool_use で schema 形式の構造化出力を強制取得する。

        Anthropic SDK の tool_choice で特定ツールを強制呼び出し → block.input を
        pydantic.model_validate でパース。プレーンテキストや ```json フェンスが
        混入しないので、堅牢にスキーマ準拠で取り出せる。
        """
        tool_def = {
            "name": tool_name,
            "description": (
                f"取引明細を分類して {schema.__name__} スキーマ形式の構造化データで返す"
            ),
            "input_schema": schema.model_json_schema(),
        }

        resp = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            tools=[tool_def],
            tool_choice={"type": "tool", "name": tool_name},
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        for block in resp.content:
            if (
                getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == tool_name
            ):
                self.log.info(
                    "classifier.tool_use_received",
                    input_tokens=resp.usage.input_tokens,
                    output_tokens=resp.usage.output_tokens,
                )
                return schema.model_validate(block.input)

        raise ClassifierError(
            "Expected tool_use block in response, got: "
            f"{[getattr(b, 'type', None) for b in resp.content]}"
        )
