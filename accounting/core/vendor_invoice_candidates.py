"""vendor-invoice タスク用の候補テーブル定義。

Gmail から取り込んだメール 1 件 × 添付 1 件 を 1 レコードとする
（本文のみ抽出した場合は attachment_id を NULL とする）。

ステータス一覧:
- pending: dry-run / 本番実行前の候補（freee 未登録）
- registered: freee に登録済み（未払金として計上中）
- reconciled: 未払金消し込み完了（対応する振込が見つかった）
- unpaid: 登録済みだが未振込（消し込み待ち）
- manual_review: 自動処理不能（partner 未登録、Vision 失敗、暗号化ZIP 等）
- excluded: 除外確定（クレカ系・自社発行など）
- failed: 何らかのエラーで処理失敗
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from accounting.core.db import Base, get_session_factory


class VendorInvoiceCandidate(Base):
    __tablename__ = "vendor_invoice_candidates"
    __table_args__ = (
        UniqueConstraint(
            "gmail_message_id",
            "gmail_attachment_id",
            name="uq_gmail_message_attachment",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    gmail_message_id: Mapped[str] = mapped_column(String(64), index=True)
    # 本文のみ抽出のときは "" (空文字) を使う（SQLite の UNIQUE は NULL を別物扱いするため）
    gmail_attachment_id: Mapped[str] = mapped_column(String(256), default="")
    received_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    sender: Mapped[str] = mapped_column(String(256))
    subject: Mapped[str] = mapped_column(Text)
    raw_pdf_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    classification: Mapped[str] = mapped_column(String(32), index=True)
    exclusion_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    extracted_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extracted_tax: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extracted_issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    extracted_due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    extracted_partner_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    extracted_bank_account: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    freee_partner_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    freee_account_item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    freee_account_item_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    freee_deal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="pending")
    reconciled_with_deal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "gmail_message_id": self.gmail_message_id,
            "gmail_attachment_id": self.gmail_attachment_id or None,
            "received_at": self.received_at,
            "sender": self.sender,
            "subject": self.subject,
            "raw_pdf_path": self.raw_pdf_path,
            "classification": self.classification,
            "exclusion_reason": self.exclusion_reason,
            "extracted_amount": self.extracted_amount,
            "extracted_tax": self.extracted_tax,
            "extracted_issue_date": self.extracted_issue_date,
            "extracted_due_date": self.extracted_due_date,
            "extracted_partner_name": self.extracted_partner_name,
            "extracted_bank_account": self.extracted_bank_account,
            "extracted_summary": self.extracted_summary,
            "freee_partner_id": self.freee_partner_id,
            "freee_account_item_id": self.freee_account_item_id,
            "freee_account_item_name": self.freee_account_item_name,
            "freee_deal_id": self.freee_deal_id,
            "status": self.status,
            "reconciled_with_deal_id": self.reconciled_with_deal_id,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def find_by_message_attachment(
    gmail_message_id: str, gmail_attachment_id: str
) -> VendorInvoiceCandidate | None:
    Session = get_session_factory()
    with Session() as s:
        stmt = select(VendorInvoiceCandidate).where(
            VendorInvoiceCandidate.gmail_message_id == gmail_message_id,
            VendorInvoiceCandidate.gmail_attachment_id == gmail_attachment_id,
        )
        return s.execute(stmt).scalar_one_or_none()


def upsert_candidate(
    gmail_message_id: str,
    gmail_attachment_id: str,
    **fields: Any,
) -> VendorInvoiceCandidate:
    """(message_id, attachment_id) で upsert。既存があれば update、なければ insert。"""
    Session = get_session_factory()
    with Session() as s:
        existing = s.execute(
            select(VendorInvoiceCandidate).where(
                VendorInvoiceCandidate.gmail_message_id == gmail_message_id,
                VendorInvoiceCandidate.gmail_attachment_id == gmail_attachment_id,
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = VendorInvoiceCandidate(
                gmail_message_id=gmail_message_id,
                gmail_attachment_id=gmail_attachment_id,
                **fields,
            )
            s.add(existing)
        else:
            for k, v in fields.items():
                setattr(existing, k, v)
        s.commit()
        s.refresh(existing)
        return existing


def list_by_status(status: str | list[str]) -> list[VendorInvoiceCandidate]:
    statuses = [status] if isinstance(status, str) else status
    Session = get_session_factory()
    with Session() as s:
        stmt = (
            select(VendorInvoiceCandidate)
            .where(VendorInvoiceCandidate.status.in_(statuses))
            .order_by(VendorInvoiceCandidate.received_at.desc())
        )
        return list(s.execute(stmt).scalars().all())


def get_by_id(candidate_id: int) -> VendorInvoiceCandidate | None:
    Session = get_session_factory()
    with Session() as s:
        return s.get(VendorInvoiceCandidate, candidate_id)


def update_status(
    candidate_id: int,
    status: str,
    **fields: Any,
) -> None:
    Session = get_session_factory()
    with Session() as s:
        row = s.get(VendorInvoiceCandidate, candidate_id)
        if row is None:
            return
        row.status = status
        for k, v in fields.items():
            setattr(row, k, v)
        s.commit()
