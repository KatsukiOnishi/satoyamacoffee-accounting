from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from accounting.core.db import Base, get_session_factory


class ExecutedOperation(Base):
    __tablename__ = "executed_operations"
    __table_args__ = (UniqueConstraint("task", "external_id", name="uq_task_external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task: Mapped[str] = mapped_column(String(128), index=True)
    external_id: Mapped[str] = mapped_column(String(256), index=True)
    run_id: Mapped[str] = mapped_column(String(128))
    freee_journal_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32))  # success / dry_run / failed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


def is_executed(task: str, external_id: str) -> bool:
    """status=success のレコードがあるときだけ True。failed/dry_run は再実行可能扱い。"""
    Session = get_session_factory()
    with Session() as s:
        stmt = select(ExecutedOperation).where(
            ExecutedOperation.task == task,
            ExecutedOperation.external_id == external_id,
            ExecutedOperation.status == "success",
        )
        return s.execute(stmt).scalar_one_or_none() is not None


def mark_executed(
    task: str,
    external_id: str,
    run_id: str,
    freee_journal_id: str | None,
    status: str,
) -> None:
    """(task, external_id) で upsert。失敗→再実行→成功のステータス更新を許す。"""
    Session = get_session_factory()
    with Session() as s:
        existing = s.execute(
            select(ExecutedOperation).where(
                ExecutedOperation.task == task,
                ExecutedOperation.external_id == external_id,
            )
        ).scalar_one_or_none()
        if existing is None:
            s.add(
                ExecutedOperation(
                    task=task,
                    external_id=external_id,
                    run_id=run_id,
                    freee_journal_id=freee_journal_id,
                    status=status,
                )
            )
        else:
            existing.run_id = run_id
            existing.freee_journal_id = freee_journal_id
            existing.status = status
        s.commit()


def get_execution(task: str, external_id: str) -> dict[str, Any] | None:
    Session = get_session_factory()
    with Session() as s:
        stmt = select(ExecutedOperation).where(
            ExecutedOperation.task == task,
            ExecutedOperation.external_id == external_id,
        )
        row = s.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        return {
            "id": row.id,
            "task": row.task,
            "external_id": row.external_id,
            "run_id": row.run_id,
            "freee_journal_id": row.freee_journal_id,
            "status": row.status,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
