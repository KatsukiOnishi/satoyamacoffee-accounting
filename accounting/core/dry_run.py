from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator

from accounting.config import settings

_dry_run_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "is_dry_run", default=settings.dry_run
)


def is_dry_run() -> bool:
    return _dry_run_var.get()


@contextmanager
def DryRunContext(enabled: bool) -> Iterator[bool]:
    token = _dry_run_var.set(enabled)
    try:
        yield enabled
    finally:
        _dry_run_var.reset(token)
