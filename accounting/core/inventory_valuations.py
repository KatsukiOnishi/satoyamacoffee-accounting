"""在庫評価額の月次スナップショット保存テーブル。

月次の在庫評価仕訳タスク (inventory_valuation) で、当月計上した評価額を
保存し、翌月の前月逆仕訳を組み立てる際に参照する。

冪等性は executed_operations 側（task="inventory_valuation", external_id="YYYY-MM"）
で管理する。本テーブルは「金額の履歴」を持つだけ。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column

from accounting.core.db import Base, get_session_factory


class InventoryValuation(Base):
    __tablename__ = "inventory_valuations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 月キー (YYYY-MM)。1月1レコードのユニーク
    month: Mapped[str] = mapped_column(String(7), unique=True, index=True)
    amount_jpy: Mapped[int] = mapped_column(Integer)
    as_of: Mapped[date] = mapped_column(Date)
    # freee の manual_journal ID。dry-run のときは None のまま
    journal_id_closing: Mapped[str | None] = mapped_column(String(64), nullable=True)
    journal_id_reversal: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


def get_by_month(month: str) -> dict[str, Any] | None:
    """月キー (YYYY-MM) で1件取得。なければ None。"""
    Session = get_session_factory()
    with Session() as s:
        row = s.execute(
            select(InventoryValuation).where(InventoryValuation.month == month)
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "id": row.id,
            "month": row.month,
            "amount_jpy": row.amount_jpy,
            "as_of": row.as_of,
            "journal_id_closing": row.journal_id_closing,
            "journal_id_reversal": row.journal_id_reversal,
            "run_id": row.run_id,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }


def upsert(
    month: str,
    amount_jpy: int,
    as_of: date,
    run_id: str,
    journal_id_closing: str | None = None,
    journal_id_reversal: str | None = None,
) -> None:
    """(month) で upsert。dry-run 中は呼ばないこと（実登録のときだけ呼ぶ）。"""
    Session = get_session_factory()
    with Session() as s:
        existing = s.execute(
            select(InventoryValuation).where(InventoryValuation.month == month)
        ).scalar_one_or_none()
        if existing is None:
            s.add(
                InventoryValuation(
                    month=month,
                    amount_jpy=amount_jpy,
                    as_of=as_of,
                    journal_id_closing=journal_id_closing,
                    journal_id_reversal=journal_id_reversal,
                    run_id=run_id,
                )
            )
        else:
            existing.amount_jpy = amount_jpy
            existing.as_of = as_of
            existing.run_id = run_id
            if journal_id_closing is not None:
                existing.journal_id_closing = journal_id_closing
            if journal_id_reversal is not None:
                existing.journal_id_reversal = journal_id_reversal
        s.commit()


def previous_month_key(month: str) -> str:
    """`2026-04` → `2026-03`、`2026-01` → `2025-12` を返す。"""
    year, mon = month.split("-")
    y, m = int(year), int(mon)
    if m == 1:
        return f"{y - 1:04d}-12"
    return f"{y:04d}-{m - 1:02d}"
