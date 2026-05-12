"""Claude Vision API wrapper for document extraction.

画像（jpg/png/heic）を読み込んで JPEG に正規化し、pydantic スキーマに沿った構造化抽出を行う。
"""
from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Type, TypeVar

import pillow_heif
from anthropic import Anthropic
from PIL import Image
from pydantic import BaseModel

from accounting.config import settings
from accounting.core.logger import get_logger

pillow_heif.register_heif_opener()

T = TypeVar("T", bound=BaseModel)


class VisionExtractor:
    def __init__(self, model: str = "claude-sonnet-4-6") -> None:
        if not settings.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY が未設定です（.env に追加してください）")
        self.client = Anthropic(api_key=settings.anthropic_api_key)
        self.model = model
        self.log = get_logger("anthropic_vision")

    def _encode_image(self, path: Path) -> tuple[str, str]:
        """HEIC/PNG/JPG を JPEG に正規化して base64 で返す。"""
        img = Image.open(path)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=85)
        return "image/jpeg", base64.standard_b64encode(buf.getvalue()).decode()

    def extract(self, image_paths: list[Path], schema: Type[T], system_prompt: str) -> T:
        """画像群を1リクエストで送り、schema 形式の pydantic モデルで返す。

        Anthropic API の `tools` 機能を使い、モデルに `extract_statement` ツールを
        強制呼び出しさせて構造化出力を得る。プレーンテキストの前置きや ```json フェンス
        が混入する余地がなく、`model_validate_json` のパース失敗を防げる。
        """
        contents: list[dict] = []
        for p in image_paths:
            media_type, data = self._encode_image(p)
            contents.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": data,
                    },
                }
            )
            self.log.info("vision.image_loaded", path=str(p), size_bytes=len(data))
        contents.append(
            {
                "type": "text",
                "text": (
                    "明細書の画像から情報を抽出し、必ず extract_statement ツールを呼び出して"
                    "結果を返してください。"
                ),
            }
        )

        tool_def = {
            "name": "extract_statement",
            "description": (
                f"明細書から構造化データを {schema.__name__} スキーマ形式で抽出する"
            ),
            "input_schema": schema.model_json_schema(),
        }

        resp = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            tools=[tool_def],
            tool_choice={"type": "tool", "name": "extract_statement"},
            system=system_prompt,
            messages=[{"role": "user", "content": contents}],
        )

        for block in resp.content:
            if (
                getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == "extract_statement"
            ):
                self.log.info(
                    "vision.tool_use_received",
                    input_tokens=resp.usage.input_tokens,
                    output_tokens=resp.usage.output_tokens,
                )
                return schema.model_validate(block.input)

        raise RuntimeError(
            "Expected tool_use block in response, got: "
            f"{[getattr(b, 'type', None) for b in resp.content]}"
        )
