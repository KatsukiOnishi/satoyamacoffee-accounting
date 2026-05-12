"""Web UI のマルチステップ操作で、抽出結果を一時保存するための pending_extractions テーブル。

GET/upload → POST/extract（抽出して保存） → ユーザーが preview 確認 → POST/register（取り出して登録）
の流れで、抽出結果を ID 経由で受け渡す。サーバ起動時に 24時間以上経過した行は削除する。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import DateTime, String, Text, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from accounting.core.db import Base, get_session_factory


class PendingExtraction(Base):
    __tablename__ = "pending_extractions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task: Mapped[str] = mapped_column(String(128))
    vendor_slug: Mapped[str] = mapped_column(String(64))
    period_yyyymm: Mapped[str] = mapped_column(String(7))
    statement_json: Mapped[str] = mapped_column(Text)
    image_paths_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


def create(
    task: str,
    vendor_slug: str,
    period_yyyymm: str,
    statement_json: str,
    image_paths: list[str],
) -> str:
    """新しい pending_extraction を作成して id を返す。"""
    extraction_id = uuid.uuid4().hex
    Session = get_session_factory()
    with Session() as s:
        s.add(
            PendingExtraction(
                id=extraction_id,
                task=task,
                vendor_slug=vendor_slug,
                period_yyyymm=period_yyyymm,
                statement_json=statement_json,
                image_paths_json=json.dumps(image_paths),
            )
        )
        s.commit()
    return extraction_id


def get(extraction_id: str) -> dict[str, Any] | None:
    Session = get_session_factory()
    with Session() as s:
        row = s.execute(
            select(PendingExtraction).where(PendingExtraction.id == extraction_id)
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "id": row.id,
            "task": row.task,
            "vendor_slug": row.vendor_slug,
            "period_yyyymm": row.period_yyyymm,
            "statement_json": row.statement_json,
            "image_paths": json.loads(row.image_paths_json),
            "created_at": row.created_at,
        }


def delete_one(extraction_id: str) -> None:
    Session = get_session_factory()
    with Session() as s:
        s.execute(delete(PendingExtraction).where(PendingExtraction.id == extraction_id))
        s.commit()


def cleanup_old(hours: int = 24) -> int:
    """指定時間以上経過した pending_extraction を削除する。削除件数を返す。"""
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    Session = get_session_factory()
    with Session() as s:
        result = s.execute(
            delete(PendingExtraction).where(PendingExtraction.created_at < cutoff)
        )
        s.commit()
        return result.rowcount or 0
