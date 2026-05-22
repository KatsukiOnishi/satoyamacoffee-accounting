"""auto-classify のモード管理（shadow / production）。

system_settings テーブルにキーを永続化する。実装は `accounting.core.auto_keiri` 側に
寄せてあり、ここはユーザー向けの呼び出しヘルパ。
"""
from __future__ import annotations

from accounting.core import auto_keiri


def get_mode() -> str:
    return auto_keiri.get_auto_classify_mode()


def set_mode(mode: str, reason: str | None = None) -> None:
    auto_keiri.set_auto_classify_mode(mode, reason=reason)


def get_thresholds() -> tuple[float, float]:
    """(high, low) のしきい値。仕様書 §5-4。"""
    return auto_keiri.get_threshold_high(), auto_keiri.get_threshold_low()
