from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from accounting.config import settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(
            settings.database_url,
            future=True,
            connect_args={"check_same_thread": False},
        )
    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionLocal


def init_db() -> None:
    # 全モデルを import してから create_all する
    from accounting.core import (  # noqa: F401
        auto_keiri,
        extractions,
        idempotency,
        inventory_valuations,
        vendor_invoice_candidates,
    )

    Base.metadata.create_all(bind=get_engine())
    # auto-keiri のデフォルト値（モード=shadow、しきい値 0.85/0.6）を保証する。
    # 既存があれば触らないので冪等。
    auto_keiri.ensure_initial_settings()
