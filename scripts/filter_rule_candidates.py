#!/usr/bin/env python3
"""journal-rules analyze の出力CSVから「user_matcher化禁止パターン」を除外する。

これらは別タスク（ar-reconcile, vendor-invoice, Shopify集計, 月次請求処理）で
処理される領域。user_matcher として登録すると Deal が重複作成される。

使い方:
    python3 scripts/filter_rule_candidates.py < /tmp/rule_candidates.csv > /tmp/rule_candidates_filtered.csv

または apply にパイプ:
    python3 scripts/filter_rule_candidates.py < /tmp/rule_candidates.csv | tee /tmp/rule_candidates_filtered.csv
    uv run accounting journal-rules apply --input /tmp/rule_candidates_filtered.csv --no-dry-run --no-interactive --batch-size 200
"""
from __future__ import annotations

import csv
import re
import sys

# 完全一致または部分一致で除外するキーワード
# 各パターンは「正規表現」として keyword 列に対して評価する
_EXCLUDED_PATTERNS = [
    # === 売掛金消込（ar-reconcile タスクが処理） ===
    r"^振込\s*カ）マゴロクオンセン",        # 株式会社孫六温泉
    r"^振込\s*サトウシヨクヒン",             # 佐藤食品株式会社
    r"^振込\s*イネトアガベ",                 # 稲とアガベ株式会社
    r"^振込\s*カ）アウトクロツプ",            # 株式会社アウトクロップ
    # === 仕入未払金消込（vendor-invoice タスクが処理） ===
    r"^振込\s*カ）ピ.エスアイ",              # 株式会社ピーエスアイ
    # === Shopify売上集計タスクが処理 ===
    r"^Shopify\b",
    r"^SHOPIFY\b",
    r"カ\)デジカ",                            # Digica（Shopify日本代理店）
    # === 月次請求処理スキルが配送料を請求書に計上 ===
    r"^配送料",
    r"^【.*納品分】配送料",
    # === 個人名カナ振込（給与仕訳タスク or 経費精算で処理）===
    # ※ ヒロシマ タケシ（地代家賃）は固定額の家賃なので除外しない（残す）
    # ※ 個人名カナは原則 user_matcher 化しないが、ここで一律除外は危険なので
    #   既知パターンのみ個別追加する方針。今は0件。
]

_EXCLUDED_REGEX = [re.compile(p) for p in _EXCLUDED_PATTERNS]


def should_exclude(keyword: str) -> bool:
    return any(rx.search(keyword) for rx in _EXCLUDED_REGEX)


def main() -> int:
    reader = csv.DictReader(sys.stdin)
    if not reader.fieldnames:
        print("ERROR: empty CSV or no header", file=sys.stderr)
        return 1
    writer = csv.DictWriter(sys.stdout, fieldnames=reader.fieldnames)
    writer.writeheader()
    kept = 0
    excluded = 0
    for row in reader:
        if should_exclude(row.get("keyword", "")):
            excluded += 1
            print(f"  EXCLUDED: {row['keyword']!r} ({row.get('suggested_account_item_name')})", file=sys.stderr)
            continue
        writer.writerow(row)
        kept += 1
    print(f"\n  kept: {kept} / excluded: {excluded}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
