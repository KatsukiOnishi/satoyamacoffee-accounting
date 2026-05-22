"""auto-keiri タスク群（ar-reconcile / auto-classify / email-digest）の永続化レイヤ。

仕様書 §4-1 の DB スキーマに対応する SQLAlchemy モデルとアクセス関数を定義する。
既存プロジェクトの慣例（`accounting/core/` に Base 継承クラスを置く、`init_db()` の
`Base.metadata.create_all` で生成）に合わせており、別途 raw SQL マイグレーションは
持たない。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from accounting.core.db import Base, get_session_factory


# ---------------- ar-reconcile ---------------- #


class ARReconcileCandidate(Base):
    __tablename__ = "ar_reconcile_candidates"
    __table_args__ = (
        UniqueConstraint("wallet_txn_id", name="uq_ar_reconcile_wallet_txn_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(128), index=True)
    run_started_at: Mapped[datetime] = mapped_column(DateTime)

    wallet_txn_id: Mapped[int] = mapped_column(Integer)
    wallet_txn_date: Mapped[date] = mapped_column(Date)
    wallet_txn_description: Mapped[str] = mapped_column(Text)
    wallet_txn_amount: Mapped[int] = mapped_column(Integer)

    matched_invoice_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matched_partner_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matched_partner_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    matched_invoice_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matched_invoice_issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    matched_invoice_due_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    status: Mapped[str] = mapped_column(String(32), index=True)
    freee_reconcile_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ---------------- auto-classify ---------------- #


class AutoClassifyCandidate(Base):
    __tablename__ = "auto_classify_candidates"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "wallet_txn_id", name="uq_auto_classify_run_wallet_txn"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(128), index=True)
    run_started_at: Mapped[datetime] = mapped_column(DateTime)
    mode: Mapped[str] = mapped_column(String(16), index=True)

    wallet_txn_id: Mapped[int] = mapped_column(Integer)
    wallet_txn_date: Mapped[date] = mapped_column(Date)
    wallet_txn_description: Mapped[str] = mapped_column(Text)
    wallet_txn_amount: Mapped[int] = mapped_column(Integer)
    wallet_txn_walletable_name: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )

    classified_account_item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    classified_account_item_name: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    classified_tax_code_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    classified_tax_code_name: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    classified_partner_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    classified_partner_name: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    classification_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    classification_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    classification_alternative: Mapped[str | None] = mapped_column(Text, nullable=True)

    action_taken: Mapped[str] = mapped_column(String(32), index=True)
    freee_deal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ---------------- system_settings ---------------- #


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    updated_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


def get_setting(key: str, default: str | None = None) -> str | None:
    Session = get_session_factory()
    with Session() as s:
        row = s.get(SystemSetting, key)
        return row.value if row else default


def set_setting(key: str, value: str, reason: str | None = None) -> None:
    Session = get_session_factory()
    with Session() as s:
        row = s.get(SystemSetting, key)
        if row is None:
            row = SystemSetting(key=key, value=value, updated_reason=reason)
            s.add(row)
        else:
            row.value = value
            row.updated_reason = reason
        s.commit()


# ---------------- notification_log ---------------- #


class NotificationLog(Base):
    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task: Mapped[str] = mapped_column(String(64), index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime)
    week_start: Mapped[date] = mapped_column(Date)
    week_end: Mapped[date] = mapped_column(Date)
    recipient: Mapped[str] = mapped_column(String(256))
    subject: Mapped[str] = mapped_column(Text)
    body_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    resend_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean)


# ---------------- アクセス関数 ---------------- #


def insert_ar_candidate(**fields: Any) -> ARReconcileCandidate:
    """ar_reconcile_candidates へ INSERT。

    (wallet_txn_id) UNIQUE 制約があるため、同じ wallet_txn を二重で書こうとした
    場合は INSERT 失敗→既存レコードの更新（ステータスのみ）に切り替える。
    """
    Session = get_session_factory()
    with Session() as s:
        existing = s.execute(
            select(ARReconcileCandidate).where(
                ARReconcileCandidate.wallet_txn_id == fields["wallet_txn_id"]
            )
        ).scalar_one_or_none()
        if existing is None:
            row = ARReconcileCandidate(**fields)
            s.add(row)
            s.commit()
            s.refresh(row)
            return row
        # 既存があれば status / matched_* を上書き
        for k, v in fields.items():
            if hasattr(existing, k) and k not in ("id", "created_at"):
                setattr(existing, k, v)
        s.commit()
        s.refresh(existing)
        return existing


def list_ar_candidates_in_range(
    start: date, end: date
) -> list[ARReconcileCandidate]:
    Session = get_session_factory()
    with Session() as s:
        stmt = (
            select(ARReconcileCandidate)
            .where(ARReconcileCandidate.wallet_txn_date >= start)
            .where(ARReconcileCandidate.wallet_txn_date <= end)
            .order_by(ARReconcileCandidate.wallet_txn_date.asc())
        )
        return list(s.execute(stmt).scalars().all())


def get_reconciled_wallet_txn_ids() -> set[int]:
    """ar-reconcile で 'reconciled' 済の wallet_txn_id 一覧。"""
    Session = get_session_factory()
    with Session() as s:
        stmt = select(ARReconcileCandidate.wallet_txn_id).where(
            ARReconcileCandidate.status == "reconciled"
        )
        return {int(row[0]) for row in s.execute(stmt).all()}


def insert_classify_candidate(**fields: Any) -> AutoClassifyCandidate:
    Session = get_session_factory()
    with Session() as s:
        existing = s.execute(
            select(AutoClassifyCandidate).where(
                AutoClassifyCandidate.run_id == fields["run_id"],
                AutoClassifyCandidate.wallet_txn_id == fields["wallet_txn_id"],
            )
        ).scalar_one_or_none()
        if existing is None:
            row = AutoClassifyCandidate(**fields)
            s.add(row)
            s.commit()
            s.refresh(row)
            return row
        for k, v in fields.items():
            if hasattr(existing, k) and k not in ("id", "created_at"):
                setattr(existing, k, v)
        s.commit()
        s.refresh(existing)
        return existing


def list_classify_candidates_in_range(
    start: date, end: date
) -> list[AutoClassifyCandidate]:
    Session = get_session_factory()
    with Session() as s:
        stmt = (
            select(AutoClassifyCandidate)
            .where(AutoClassifyCandidate.wallet_txn_date >= start)
            .where(AutoClassifyCandidate.wallet_txn_date <= end)
            .order_by(AutoClassifyCandidate.wallet_txn_date.asc())
        )
        return list(s.execute(stmt).scalars().all())


def insert_notification_log(**fields: Any) -> NotificationLog:
    Session = get_session_factory()
    with Session() as s:
        row = NotificationLog(**fields)
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


# ---------------- mode helpers ---------------- #

AUTO_CLASSIFY_MODE_KEY = "auto_classify_mode"
AUTO_CLASSIFY_THRESHOLD_HIGH_KEY = "auto_classify_threshold_high"
AUTO_CLASSIFY_THRESHOLD_LOW_KEY = "auto_classify_threshold_low"

MODE_SHADOW = "shadow"
MODE_PRODUCTION = "production"
VALID_MODES = {MODE_SHADOW, MODE_PRODUCTION}


def get_auto_classify_mode() -> str:
    """auto-classify の現在モード（未設定なら shadow）。"""
    return get_setting(AUTO_CLASSIFY_MODE_KEY, MODE_SHADOW) or MODE_SHADOW


def set_auto_classify_mode(mode: str, reason: str | None = None) -> None:
    if mode not in VALID_MODES:
        raise ValueError(f"mode は {sorted(VALID_MODES)} のいずれかにしてください: {mode!r}")
    set_setting(AUTO_CLASSIFY_MODE_KEY, mode, reason)


def get_threshold_high() -> float:
    raw = get_setting(AUTO_CLASSIFY_THRESHOLD_HIGH_KEY, "0.85")
    try:
        return float(raw or "0.85")
    except ValueError:
        return 0.85


def get_threshold_low() -> float:
    raw = get_setting(AUTO_CLASSIFY_THRESHOLD_LOW_KEY, "0.6")
    try:
        return float(raw or "0.6")
    except ValueError:
        return 0.6


def ensure_initial_settings() -> None:
    """初回 init 時にデフォルト値を入れる（既存があれば触らない）。"""
    if get_setting(AUTO_CLASSIFY_MODE_KEY) is None:
        set_setting(AUTO_CLASSIFY_MODE_KEY, MODE_SHADOW, "initial setup")
    if get_setting(AUTO_CLASSIFY_THRESHOLD_HIGH_KEY) is None:
        set_setting(AUTO_CLASSIFY_THRESHOLD_HIGH_KEY, "0.85", "initial setup")
    if get_setting(AUTO_CLASSIFY_THRESHOLD_LOW_KEY) is None:
        set_setting(AUTO_CLASSIFY_THRESHOLD_LOW_KEY, "0.6", "initial setup")


__all__ = [
    "ARReconcileCandidate",
    "AutoClassifyCandidate",
    "SystemSetting",
    "NotificationLog",
    "AUTO_CLASSIFY_MODE_KEY",
    "MODE_SHADOW",
    "MODE_PRODUCTION",
    "VALID_MODES",
    "insert_ar_candidate",
    "list_ar_candidates_in_range",
    "get_reconciled_wallet_txn_ids",
    "insert_classify_candidate",
    "list_classify_candidates_in_range",
    "insert_notification_log",
    "get_setting",
    "set_setting",
    "get_auto_classify_mode",
    "set_auto_classify_mode",
    "get_threshold_high",
    "get_threshold_low",
    "ensure_initial_settings",
]
