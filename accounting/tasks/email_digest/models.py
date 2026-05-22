"""email-digest タスクのデータモデル（Pydantic）。"""
from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field


class ARLine(BaseModel):
    partner_name: str | None
    amount: int
    issue_date: date | None = None
    txn_date: date | None = None
    status: str                          # reconciled / unmatched / multiple_matches / failed
    description: str | None = None
    deal_id: int | None = None


class ClassifyLine(BaseModel):
    date: date
    description: str
    amount: int
    account_item: str | None = None
    tax_code: str | None = None
    confidence: float = 0.0
    action_taken: str = ""
    reason: str | None = None


class DigestSection(BaseModel):
    title: str
    counts: dict[str, int] = Field(default_factory=dict)
    lines: list[Any] = Field(default_factory=list)


class WeeklyDigest(BaseModel):
    week_start: date
    week_end: date
    iso_week: str                # '2026-W21'
    mode: str                    # shadow / production

    ar_reconciled: list[ARLine] = Field(default_factory=list)
    ar_unmatched: list[ARLine] = Field(default_factory=list)
    ar_multiple_matches: list[ARLine] = Field(default_factory=list)
    ar_failed: list[ARLine] = Field(default_factory=list)

    classify_registered: list[ClassifyLine] = Field(default_factory=list)
    classify_review_required: list[ClassifyLine] = Field(default_factory=list)
    classify_skipped: list[ClassifyLine] = Field(default_factory=list)
    classify_shadow_logged: list[ClassifyLine] = Field(default_factory=list)
    classify_failed: list[ClassifyLine] = Field(default_factory=list)

    @property
    def ar_reconciled_total(self) -> int:
        return sum(line.amount for line in self.ar_reconciled)

    @property
    def total_success(self) -> int:
        """件名用「成功」件数（ar reconciled + classify registered）。"""
        return len(self.ar_reconciled) + len(self.classify_registered)

    @property
    def total_review_required(self) -> int:
        """件名用「要確認」件数。"""
        return (
            len(self.ar_unmatched)
            + len(self.ar_multiple_matches)
            + len(self.ar_failed)
            + len(self.classify_review_required)
            + len(self.classify_failed)
        )
