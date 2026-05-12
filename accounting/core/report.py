from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from accounting.core import notifier
from accounting.core.logger import get_logger


def make_run_id(task: str) -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    short = uuid.uuid4().hex[:8]
    return f"{task}-{ts}-{short}"


@dataclass
class _Entry:
    item: Any
    detail: str | None = None


@dataclass
class RunReport:
    task: str
    run_id: str
    successes: list[_Entry] = field(default_factory=list)
    failures: list[_Entry] = field(default_factory=list)
    warnings: list[_Entry] = field(default_factory=list)

    def add_success(self, item: Any, detail: str | None = None) -> None:
        self.successes.append(_Entry(item=item, detail=detail))

    def add_failure(self, item: Any, error: Exception | str) -> None:
        detail = str(error) if isinstance(error, Exception) else error
        self.failures.append(_Entry(item=item, detail=detail))

    def add_warning(self, item: Any, reason: str) -> None:
        self.warnings.append(_Entry(item=item, detail=reason))

    def as_summary(self) -> dict[str, Any]:
        return {
            "success_count": len(self.successes),
            "failure_count": len(self.failures),
            "warning_count": len(self.warnings),
            "failures": [
                {"item": str(e.item), "detail": e.detail} for e in self.failures
            ],
            "warnings": [
                {"item": str(e.item), "detail": e.detail} for e in self.warnings
            ],
        }

    def finalize(self) -> dict[str, Any]:
        logger = get_logger(self.task)
        summary = self.as_summary()
        logger.info("run.finalize", run_id=self.run_id, **summary)
        # 成功サマリの通知（NOTIFY_ON_SUCCESS=true のときだけ送信される）
        notifier.notify_summary(self.task, self.run_id, summary)
        return summary


def new_report(task: str) -> RunReport:
    return RunReport(task=task, run_id=make_run_id(task))
