"""shopify-sales 用の dataclass。

集計途中の表現を 1 ファイルに集約する。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class PartnerSummary:
    """gateway / partner 単位の月次集計。"""

    partner_id: int
    partner_name: str
    order_count: int = 0
    gross: int = 0  # 売上総額 (税込、返金控除後)
    fee: int = 0    # 決済手数料
    gateways: set[str] = field(default_factory=set)

    @property
    def net(self) -> int:
        return self.gross - self.fee


@dataclass
class MonthlySummary:
    """月次 1 本の Deal 化に必要な情報を全て持つ。"""

    year: int
    month: int  # 1-12
    period_start_jst: date
    period_end_jst: date  # 月末日 (issue_date に使う)

    order_count: int = 0
    excluded_count: int = 0
    by_partner: dict[int, PartnerSummary] = field(default_factory=dict)

    # gateway 別の警告（4月実績ゼロの Amazon Pay / PayPay が出たら詰める）
    warnings: list[str] = field(default_factory=list)

    @property
    def total_gross(self) -> int:
        return sum(p.gross for p in self.by_partner.values())

    @property
    def total_fee(self) -> int:
        return sum(p.fee for p in self.by_partner.values())

    @property
    def total_net(self) -> int:
        return self.total_gross - self.total_fee
