from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars

from accounting.config import settings

_configured = False


def _log_file_path() -> Path:
    return settings.logs_dir / f"{date.today().isoformat()}.jsonl"


def _configure() -> None:
    global _configured
    if _configured:
        return

    level_name = settings.log_level.upper()
    level = getattr(logging, level_name, logging.INFO)

    log_path = _log_file_path()
    file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(message)s"))

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter("%(message)s"))

    root = logging.getLogger()
    # 既存ハンドラを掃除して二重出力を防ぐ
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    root.setLevel(level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=False),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    _configured = True


def get_logger(task: str) -> structlog.stdlib.BoundLogger:
    _configure()
    return structlog.get_logger().bind(task=task)


def bind_run(task: str, run_id: str) -> None:
    _configure()
    clear_contextvars()
    bind_contextvars(task=task, run_id=run_id)


def unbind_run() -> None:
    clear_contextvars()
