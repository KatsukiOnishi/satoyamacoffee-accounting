"""自動仕訳ルール整備タスク。

過去取引（deals）を分析して `user_matchers` API のルール候補を生成し、CSV 経由で適用する。

freee 仕様の要点（user_matchers POST のフィールド）:
- description: マッチ対象キーワード（取引摘要に対して condition で照合）
- condition: 0=部分一致 / 1=前方 / 2=後方 / 3=完全 / 4=指定なし
- entry_side_str: income / expense
- act: 0=manual_standard（推測のみ） / 1=auto_standard（自動確定） / ...
- account_item_name / tax_name / partner_name: すべて「名前」文字列
- priority, active も必須
"""
from __future__ import annotations

import csv
import hashlib
import re
from collections import Counter
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from accounting.connectors.freee import FreeeClient
from accounting.core.dry_run import is_dry_run
from accounting.core.idempotency import is_executed, mark_executed
from accounting.core.logger import get_logger

log = get_logger("journal_rules")


# ---- データモデル ----


class RuleCandidate(BaseModel):
    keyword: str
    condition: int = 0
    entry_side_str: str  # "income" or "expense"
    partner_name: str | None = None
    suggested_account_item_name: str
    suggested_tax_name: str = ""
    act: int = 0  # 0=manual_standard（安全側）
    occurrence: int
    consistency: float  # 0.0-1.0
    # 同一キーワードで金額違いの取引は1ルールにまとめ、user_matcher の min_amount/max_amount で帯を表現する
    min_amount: int | None = None
    max_amount: int | None = None
    sample_amounts: list[int] = Field(default_factory=list)
    sample_descriptions: list[str] = Field(default_factory=list)


class ApplyResult(BaseModel):
    created: list[dict[str, Any]] = Field(default_factory=list)
    skipped_duplicates: list[dict[str, Any]] = Field(default_factory=list)
    failed: list[dict[str, Any]] = Field(default_factory=list)


# ---- 純粋関数（テスト対象） ----


_DATE_PATTERNS = [
    re.compile(r"\d{4}[/\-\.]\d{1,2}[/\-\.]\d{1,2}"),  # 2026/3/15, 2026-03-01
    re.compile(r"\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}"),  # 3/15/2026
    re.compile(r"\d{4}年\d{1,2}月\d{1,2}日?"),  # 2026年3月15日
]


def normalize_description(text: str | None) -> str:
    """摘要を正規化する。

    1. 日付パターン除去（YYYY/M/D、YYYY-MM-DD、YYYY.MM.DD、和暦年月日）
    2. 残った連続数字を除去（明細番号・金額の埋め込み等）
    3. 区切り記号 [_,;|] を空白化
    4. 空白正規化
    5. 20文字で切る
    """
    if not text:
        return ""
    s = text.strip()
    for pat in _DATE_PATTERNS:
        s = pat.sub("", s)
    s = re.sub(r"\d+", "", s)
    s = re.sub(r"[_,;|]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:20]


def amount_band(amount: int) -> str:
    """金額帯（符号は無視、絶対値で判定）。"""
    a = abs(int(amount or 0))
    if a == 0:
        return "0"
    if a < 100:
        return "0-100"
    if a < 1000:
        return "100-1000"
    if a < 10000:
        return "1000-10000"
    if a < 100000:
        return "10000-100000"
    if a < 1000000:
        return "100000-1M"
    return "1M+"


GroupKey = tuple[str, str, str]  # (partner_name, keyword, entry_side)


def build_id_name_map(
    items: list[dict[str, Any]], *, key: str = "id", value: str = "name"
) -> dict[int, str]:
    """freee マスタの `[{id, name}, ...]` を `{id: name}` 辞書化する。"""
    out: dict[int, str] = {}
    for it in items:
        k = it.get(key)
        v = it.get(value)
        if k is not None and v:
            try:
                out[int(k)] = str(v)
            except (TypeError, ValueError):
                continue
    return out


def group_deal_details(
    deals: list[dict[str, Any]],
    *,
    partner_map: dict[int, str] | None = None,
    account_item_map: dict[int, str] | None = None,
    tax_map: dict[int, str] | None = None,
) -> dict[GroupKey, list[dict[str, Any]]]:
    """deals を details レベルにフラット化し、(partner, 正規化摘要, 金額帯, 入出金) でグルーピングする。

    freee の deals API は `partner_id` / `account_item_id` / `tax_code` のみ返し、
    名前は返さないため、マスタ照会で解決したマップを受け取る。
    マップが渡されない場合は、deal 内の `*_name` を直接見る（テスト用フォールバック）。
    """
    partner_map = partner_map or {}
    account_item_map = account_item_map or {}
    tax_map = tax_map or {}

    groups: dict[GroupKey, list[dict[str, Any]]] = {}
    for deal in deals:
        partner_id = int(deal.get("partner_id") or 0)
        partner_name = (
            deal.get("partner_name") or partner_map.get(partner_id, "") or ""
        ).strip()
        deal_type = deal.get("type") or ""
        entry_side = "income" if deal_type == "income" else "expense"
        deal_desc = deal.get("ref_number") or deal.get("description") or ""
        for d in deal.get("details", []) or []:
            raw = (d.get("description") or deal_desc or "").strip()
            keyword = normalize_description(raw)
            if not keyword:
                continue
            account_item_name = (
                d.get("account_item_name")
                or account_item_map.get(int(d.get("account_item_id") or 0), "")
                or ""
            ).strip()
            tax_name = (
                d.get("tax_name")
                or tax_map.get(int(d.get("tax_code") or 0), "")
                or ""
            ).strip()
            key: GroupKey = (partner_name, keyword, entry_side)
            groups.setdefault(key, []).append(
                {
                    "deal_id": deal.get("id"),
                    "raw_description": raw,
                    "amount": int(d.get("amount") or 0),
                    "account_item_name": account_item_name,
                    "tax_name": tax_name,
                }
            )
    return groups


def extract_candidates(
    groups: dict[GroupKey, list[dict[str, Any]]],
    *,
    min_occurrence: int,
    consistency_threshold: float,
) -> list[RuleCandidate]:
    """グループから候補を抽出する。

    採用条件:
    - 出現回数 >= min_occurrence
    - 最頻勘定科目の一貫率 >= consistency_threshold (0.0-1.0)
    """
    candidates: list[RuleCandidate] = []
    for (partner_name, keyword, entry_side), entries in groups.items():
        if len(entries) < min_occurrence:
            continue
        ai_counts = Counter(e["account_item_name"] for e in entries if e["account_item_name"])
        if not ai_counts:
            continue
        top_ai, top_count = ai_counts.most_common(1)[0]
        consistency = top_count / len(entries)
        if consistency < consistency_threshold:
            continue
        # 最頻勘定科目を持つエントリの中で最頻税区分
        tax_counts = Counter(
            e["tax_name"]
            for e in entries
            if e["account_item_name"] == top_ai and e["tax_name"]
        )
        top_tax = tax_counts.most_common(1)[0][0] if tax_counts else ""

        # min/max は全エントリの金額（絶対値、wallet_txn の出金は負で来るため）
        amounts = [abs(int(e["amount"])) for e in entries if e.get("amount") is not None]
        amt_min = min(amounts) if amounts else None
        amt_max = max(amounts) if amounts else None

        sample_entries = entries[:3]
        candidates.append(
            RuleCandidate(
                keyword=keyword,
                condition=0,
                entry_side_str=entry_side,
                partner_name=partner_name or None,
                suggested_account_item_name=top_ai,
                suggested_tax_name=top_tax,
                act=0,
                occurrence=len(entries),
                consistency=round(consistency, 4),
                min_amount=amt_min,
                max_amount=amt_max,
                sample_amounts=[e["amount"] for e in sample_entries],
                sample_descriptions=[e["raw_description"] for e in sample_entries],
            )
        )
    # 出現回数の多い順に並べる（業務的に重要なものが先頭）
    candidates.sort(key=lambda c: (c.occurrence, c.consistency), reverse=True)
    return candidates


def collect_wallet_txn_with_deal(
    wallet_txns: list[dict[str, Any]],
    deals: list[dict[str, Any]],
    *,
    partner_map: dict[int, str] | None = None,
    account_item_map: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    """wallet_txn と、それを支払元とする deal の details[0] を結合した中間レコードを返す。

    紐付けキー: `(walletable_type, walletable_id, date, amount)` の4-tuple。
    deal.payments[] に同じ4フィールドがあるので、それを index 化して wallet_txn 側からルックアップする。
    紐づく deal が見つからない wallet_txn（未確定明細など）はスキップする。
    """
    partner_map = partner_map or {}
    account_item_map = account_item_map or {}

    payment_index: dict[tuple, dict[str, Any]] = {}
    for deal in deals:
        for p in deal.get("payments") or []:
            key = (
                p.get("from_walletable_type"),
                p.get("from_walletable_id"),
                p.get("date"),
                p.get("amount"),
            )
            payment_index.setdefault(key, deal)

    records: list[dict[str, Any]] = []
    for wt in wallet_txns:
        key = (
            wt.get("walletable_type"),
            wt.get("walletable_id"),
            wt.get("date"),
            wt.get("amount"),
        )
        deal = payment_index.get(key)
        if deal is None:
            continue
        details = deal.get("details") or []
        if not details:
            continue
        d0 = details[0]

        partner_id = int(deal.get("partner_id") or 0)
        partner_name = (
            deal.get("partner_name") or partner_map.get(partner_id, "") or ""
        ).strip()
        account_item_name = (
            d0.get("account_item_name")
            or account_item_map.get(int(d0.get("account_item_id") or 0), "")
            or ""
        ).strip()
        tax_name = (d0.get("tax_name") or "").strip()

        # entry_side は wallet_txn.entry_side をそのまま採用する（user_matchers API の SoT）。
        # 業務的には deal.type の方が自然に見えるが、user_matcher のマッチング絞り込みは
        # wallet_txn レベルで行われるため、wallet_txn.entry_side と一致しないとルールが発火しない。
        # freee の口座記法（クレカ口座=負債口座は残高増 → income）に従って記録された値を
        # そのまま採用することで、ルールが確実に freee 側で適用される。
        entry_side = "income" if wt.get("entry_side") == "income" else "expense"

        records.append(
            {
                "wallet_txn_id": wt.get("id"),
                "deal_id": deal.get("id"),
                "description": (wt.get("description") or "").strip(),
                "amount": int(wt.get("amount") or 0),
                "date": wt.get("date"),
                "entry_side": entry_side,
                "walletable_type": wt.get("walletable_type"),
                "walletable_id": wt.get("walletable_id"),
                "partner_id": partner_id,
                "partner_name": partner_name,
                "account_item_name": account_item_name,
                "tax_name": tax_name,
            }
        )
    return records


def group_wallet_txn_records(records: list[dict[str, Any]]) -> dict[GroupKey, list[dict[str, Any]]]:
    """`collect_wallet_txn_with_deal` の結果を (partner, 正規化摘要, 入出金) でグルーピング。

    金額帯はグルーピング軸から外し、候補単位で min/max を保持して user_matcher の
    min_amount/max_amount に渡す方針（同一キーワードを金額違いで分裂させない）。
    """
    groups: dict[GroupKey, list[dict[str, Any]]] = {}
    for r in records:
        keyword = normalize_description(r.get("description"))
        if not keyword:
            continue
        partner_name = (r.get("partner_name") or "").strip()
        entry_side = r.get("entry_side") or "expense"
        key: GroupKey = (partner_name, keyword, entry_side)
        groups.setdefault(key, []).append(
            {
                "deal_id": r.get("deal_id"),
                "raw_description": r.get("description") or "",
                "amount": int(r.get("amount") or 0),
                "account_item_name": r.get("account_item_name") or "",
                "tax_name": r.get("tax_name") or "",
            }
        )
    return groups


def _matcher_external_id(keyword: str, condition: int, entry_side_str: str) -> str:
    h = hashlib.sha256(f"{keyword}|{condition}|{entry_side_str}".encode("utf-8")).hexdigest()[:12]
    return f"matcher-{h}"


def _existing_matcher_keys(existing: list[dict[str, Any]]) -> set[tuple[str, int, str]]:
    keys: set[tuple[str, int, str]] = set()
    for m in existing:
        keys.add(
            (
                (m.get("description") or "").strip(),
                int(m.get("condition") or 0),
                (m.get("entry_side_str") or "").strip(),
            )
        )
    return keys


# 勘定科目 → デフォルト税区分マッピング。
# freee user_matchers API は act=0/1 のとき tax_name が必須。
# wallet_txn のみで deal が紐付かない候補（extract_candidates で top_tax="" になる）には
# フォールバック値が必要なため、業務的に明らかな科目だけ事前定義する。
_DEFAULT_TAX_BY_ACCOUNT_ITEM = {
    # 営業外収益・費用（利息系）
    "受取利息": "非課売上",
    "支払利息": "非課仕入",
    # B/S 科目（金銭移動、消費税対象外）
    "短期借入金": "対象外",
    "長期借入金": "対象外",
    "借入金": "対象外",
    "未払金": "対象外",
    "預り金": "対象外",
    "買掛金": "対象外",
    "売掛金": "対象外",
    "立替金": "対象外",
    "仮払金": "対象外",
    "仮受金": "対象外",
    # 社会保険料（不課税）
    "法定福利費": "対象外",
    # 人件費（user_matcher 化禁止対象だが念のため）
    "給料手当": "対象外",
    "役員報酬": "対象外",
    "賞与": "対象外",
}


def default_tax_name(account_item_name: str, entry_side: str) -> str:
    """suggested_tax_name が空の候補のためのデフォルト税区分を返す。

    1. 勘定科目別マッピング（B/S 科目・社会保険・利息など消費税対象外）を優先
    2. fallback: income → 「課税売上10%」, expense → 「課対仕入10%」

    NOTE: freee API が受け付ける税区分名は **スペースなしの形式**。
    UI で表示される「課対仕入（控80）10%」「課対仕入 10%」（半角スペース入り）は
    どちらも API で弾かれる（既存ルール取得結果と複数の 400 エラーから検証済み）。
    """
    if account_item_name in _DEFAULT_TAX_BY_ACCOUNT_ITEM:
        return _DEFAULT_TAX_BY_ACCOUNT_ITEM[account_item_name]
    if entry_side == "income":
        return "課税売上10%"
    return "課対仕入10%"


def _build_payload(c: RuleCandidate, *, priority: int = 50) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "act": c.act,
        "active": True,
        "condition": c.condition,
        "description": c.keyword,
        "entry_side_str": c.entry_side_str,
        "priority": priority,
        "account_item_name": c.suggested_account_item_name,
    }
    # freee は act=0/1 で tax_name 必須なので、空ならフォールバック
    payload["tax_name"] = c.suggested_tax_name or default_tax_name(
        c.suggested_account_item_name, c.entry_side_str
    )
    if c.partner_name:
        payload["partner_name"] = c.partner_name
    if c.min_amount is not None:
        payload["min_amount"] = c.min_amount
    if c.max_amount is not None:
        payload["max_amount"] = c.max_amount
    return payload


def apply_rule_candidates(
    candidates: list[RuleCandidate],
    *,
    existing_matchers: list[dict[str, Any]],
    valid_account_item_names: set[str],
    freee: FreeeClient,
    run_id: str,
    confirm: Callable[[RuleCandidate, dict[str, Any]], bool] | None = None,
) -> ApplyResult:
    """候補リストを user_matchers API で作成する。

    冪等性:
    - 既存 user_matcher と (description, condition, entry_side_str) で衝突 → skip
    - 過去に同じ external_id で成功登録済み → skip
    - account_item_name が freee に存在しない → failed
    - confirm(c, payload) が False を返したら skip（CLI の interactive 用）

    dry-run 時は実 API を叩かず、mark_executed もしない（rehearsal を本番 idempotency に混ぜない）。
    """
    result = ApplyResult()
    existing_keys = _existing_matcher_keys(existing_matchers)

    for c in candidates:
        external_id = _matcher_external_id(c.keyword, c.condition, c.entry_side_str)
        key = (c.keyword, c.condition, c.entry_side_str)

        if is_executed("journal_rules", external_id):
            result.skipped_duplicates.append(
                {
                    "keyword": c.keyword,
                    "reason": "already executed (local)",
                    "external_id": external_id,
                }
            )
            continue
        if key in existing_keys:
            result.skipped_duplicates.append(
                {
                    "keyword": c.keyword,
                    "reason": "duplicate in freee",
                    "external_id": external_id,
                }
            )
            continue
        if c.suggested_account_item_name not in valid_account_item_names:
            result.failed.append(
                {
                    "keyword": c.keyword,
                    "reason": f"account_item_name not found in freee: {c.suggested_account_item_name!r}",
                }
            )
            continue

        payload = _build_payload(c)

        if confirm is not None and not confirm(c, payload):
            result.skipped_duplicates.append(
                {"keyword": c.keyword, "reason": "skipped by user"}
            )
            continue

        try:
            res = freee.create_user_matcher(payload, external_id=external_id, task="journal_rules")
            is_real = not res.get("dry_run")
            if is_real:
                mark_executed(
                    "journal_rules",
                    external_id,
                    run_id,
                    str(res.get("id") or ""),
                    "success",
                )
            result.created.append(
                {
                    "keyword": c.keyword,
                    "matcher_id": res.get("id"),
                    "dry_run": not is_real,
                    "external_id": external_id,
                }
            )
        except Exception as e:
            log.exception("journal_rules.apply_failed", keyword=c.keyword, error=str(e))
            result.failed.append({"keyword": c.keyword, "error": str(e)})

    return result


# ---- CSV I/O ----


CSV_FIELDS = [
    "keyword",
    "condition",
    "entry_side_str",
    "partner_name",
    "suggested_account_item_name",
    "suggested_tax_name",
    "act",
    "occurrence",
    "consistency",
    "min_amount",
    "max_amount",
    "sample_amounts",
    "sample_descriptions",
]


def bulk_update_rules(
    freee: Any,
    *,
    account_filter: str | None = None,
    new_act: int | None = None,
    new_account_item_name: str | None = None,
    new_tax_name: str | None = None,
    entry_side_filter: str | None = None,
    min_occurrence_filter: int | None = None,
    csv_path: Path | None = None,
    exclude_ids: list[int] | None = None,
    interactive: bool = True,
    console: Any = None,
) -> dict[str, list]:
    """freee 上の既存 user_matchers を一括更新する。

    Args:
        freee: FreeeClient
        account_filter: account_item_name で対象を絞る（例: "売上高"）
        new_act: 新しい act 値（0=manual, 1=auto）
        new_account_item_name: 勘定科目を変更（例: "売上高" → "売掛金"）
        new_tax_name: 税区分を変更
        entry_side_filter: "income" / "expense" で絞る
        min_occurrence_filter: CSV と join して occurrence >= 閾値 のものだけ対象
        csv_path: min_occurrence_filter 使用時に必要（rule_candidates.csv）
        interactive: True なら1件ずつ y/n 確認
        console: rich.Console
    """
    from rich.console import Console
    from rich.table import Table

    log = get_logger("journal_rules")
    console = console or Console()

    matchers = freee.list_user_matchers()
    log.info("journal_rules.update.fetched", total=len(matchers))

    occurrence_map: dict[tuple[str, str], int] = {}
    if min_occurrence_filter is not None:
        if not csv_path or not csv_path.exists():
            raise ValueError("--min-occurrence 使用時は --csv で rule_candidates.csv を指定する必要があります")
        with csv_path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = (row["keyword"], row["entry_side_str"])
                occurrence_map[key] = max(occurrence_map.get(key, 0), int(row.get("occurrence", 0)))

    exclude_set = set(exclude_ids or [])
    targets: list[dict] = []
    for m in matchers:
        if m.get("id") in exclude_set:
            continue
        if account_filter and m.get("account_item_name") != account_filter:
            continue
        if entry_side_filter and m.get("entry_side_str") != entry_side_filter:
            continue
        if min_occurrence_filter is not None:
            occ = occurrence_map.get((m.get("description", ""), m.get("entry_side_str", "")), 0)
            if occ < min_occurrence_filter:
                continue
        targets.append(m)

    log.info("journal_rules.update.filtered", targets=len(targets))

    updated: list = []
    skipped: list = []
    failed: list = []

    for m in targets:
        # 既存値を維持しつつ、変更フィールドを上書き
        payload: dict[str, Any] = {}
        for k in (
            "act",
            "active",
            "condition",
            "description",
            "entry_side_str",
            "priority",
            "account_item_name",
            "tax_name",
            "partner_name",
            "min_amount",
            "max_amount",
        ):
            if m.get(k) is not None:
                payload[k] = m[k]
        if new_act is not None:
            payload["act"] = new_act
        if new_account_item_name is not None:
            payload["account_item_name"] = new_account_item_name
        if new_tax_name is not None:
            payload["tax_name"] = new_tax_name

        if interactive:
            t = Table(title=f"更新候補 (matcher_id={m.get('id')})", show_header=True)
            t.add_column("項目"); t.add_column("現在"); t.add_column("変更後")
            t.add_row("description", str(m.get("description")), str(payload.get("description")))
            t.add_row("entry_side_str", str(m.get("entry_side_str")), str(payload.get("entry_side_str")))
            t.add_row("act", str(m.get("act")), str(payload.get("act")))
            t.add_row("account_item_name", str(m.get("account_item_name")), str(payload.get("account_item_name")))
            t.add_row("tax_name", str(m.get("tax_name")), str(payload.get("tax_name")))
            console.print(t)
            try:
                ans = input("更新しますか？ [y/N]: ").strip().lower()
            except EOFError:
                ans = "n"
            if ans not in ("y", "yes"):
                skipped.append({"id": m.get("id"), "reason": "user_skipped"})
                continue

        try:
            result = freee.update_user_matcher(m["id"], payload)
            updated.append({"id": m.get("id"), "result": result})
            log.info("journal_rules.update.applied", matcher_id=m.get("id"))
        except Exception as e:
            log.exception("journal_rules.update.failed", matcher_id=m.get("id"), error=str(e))
            failed.append({"id": m.get("id"), "error": str(e)})

    return {"updated": updated, "skipped": skipped, "failed": failed}


def candidates_to_csv(candidates: list[RuleCandidate], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for c in candidates:
            d = c.model_dump()
            d["partner_name"] = d.get("partner_name") or ""
            d["min_amount"] = "" if d.get("min_amount") is None else d["min_amount"]
            d["max_amount"] = "" if d.get("max_amount") is None else d["max_amount"]
            d["sample_amounts"] = ";".join(str(a) for a in d["sample_amounts"])
            d["sample_descriptions"] = " | ".join(d["sample_descriptions"])
            w.writerow(d)


def csv_to_candidates(path: Path) -> list[RuleCandidate]:
    candidates: list[RuleCandidate] = []
    with path.open(encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            partner_name = (row.get("partner_name") or "").strip() or None
            sample_amounts = [
                int(x) for x in (row.get("sample_amounts") or "").split(";") if x.strip()
            ]
            sample_descriptions = [
                s.strip()
                for s in (row.get("sample_descriptions") or "").split(" | ")
                if s.strip()
            ]
            min_amount = row.get("min_amount", "")
            max_amount = row.get("max_amount", "")
            candidates.append(
                RuleCandidate(
                    keyword=row["keyword"],
                    condition=int(row.get("condition") or 0),
                    entry_side_str=row["entry_side_str"],
                    partner_name=partner_name,
                    suggested_account_item_name=row["suggested_account_item_name"],
                    suggested_tax_name=row.get("suggested_tax_name") or "",
                    act=int(row.get("act") or 0),
                    occurrence=int(row.get("occurrence") or 0),
                    consistency=float(row.get("consistency") or 0.0),
                    min_amount=int(min_amount) if str(min_amount).strip() else None,
                    max_amount=int(max_amount) if str(max_amount).strip() else None,
                    sample_amounts=sample_amounts,
                    sample_descriptions=sample_descriptions,
                )
            )
    return candidates


# ---- analyze ラッパ（freee API を叩く側、CLI から呼ばれる） ----


def _merge_groups(
    *group_dicts: dict[GroupKey, list[dict[str, Any]]],
) -> dict[GroupKey, list[dict[str, Any]]]:
    out: dict[GroupKey, list[dict[str, Any]]] = {}
    for g in group_dicts:
        for k, v in g.items():
            out.setdefault(k, []).extend(v)
    return out


def run_analyze(
    *,
    months_back: int = 12,
    min_occurrence: int = 3,
    consistency_threshold: float = 1.0,
    source: str = "both",  # "deals" / "wallet_txns" / "both"
    freee: FreeeClient | None = None,
) -> list[RuleCandidate]:
    """freee から過去 N ヶ月分の取引を取得して候補リストを返す。

    `source`:
      - "deals": deals.details の description を見る（income 側で機能）
      - "wallet_txns": wallet_txns.description を見る（クレカ等の expense 側で機能）
      - "both": 両方を結合（デフォルト）
    """
    if source not in {"deals", "wallet_txns", "both"}:
        raise ValueError(f"source must be one of deals/wallet_txns/both, got {source!r}")

    today = date.today()
    start = (today.replace(day=1) - timedelta(days=months_back * 31)).replace(day=1)
    end = today

    owned = freee is None
    freee = freee or FreeeClient()
    try:
        deals = freee.list_deals(
            start_issue_date=start.isoformat(),
            end_issue_date=end.isoformat(),
        )
        partners = freee.list_partners()
        account_items = freee.get_account_items()

        wallet_txns: list[dict[str, Any]] = []
        walletables: list[dict[str, Any]] = []
        if source in {"wallet_txns", "both"}:
            walletables = freee.list_walletables()
            for w in walletables:
                wt = freee.list_wallet_txns(
                    walletable_type=w.get("type"),
                    walletable_id=int(w.get("id")),
                    start_date=start.isoformat(),
                    end_date=end.isoformat(),
                )
                wallet_txns.extend(wt)
                log.info(
                    "journal_rules.analyze.wallet_txns_page",
                    walletable_id=w.get("id"),
                    walletable_type=w.get("type"),
                    walletable_name=w.get("name"),
                    txns=len(wt),
                )
    finally:
        if owned:
            freee.close()

    partner_map = build_id_name_map(partners)
    account_item_map = build_id_name_map(account_items)

    log.info(
        "journal_rules.analyze.fetched",
        deals=len(deals),
        wallet_txns=len(wallet_txns),
        walletables=len(walletables),
        partners=len(partner_map),
        account_items=len(account_item_map),
        source=source,
        start=start.isoformat(),
        end=end.isoformat(),
    )

    parts: list[dict[GroupKey, list[dict[str, Any]]]] = []

    if source in {"deals", "both"}:
        parts.append(
            group_deal_details(
                deals,
                partner_map=partner_map,
                account_item_map=account_item_map,
            )
        )

    if source in {"wallet_txns", "both"}:
        records = collect_wallet_txn_with_deal(
            wallet_txns,
            deals,
            partner_map=partner_map,
            account_item_map=account_item_map,
        )
        matched_rate = (len(records) / len(wallet_txns)) if wallet_txns else 0.0
        log.info(
            "journal_rules.analyze.wallet_txn_matched",
            wallet_txns=len(wallet_txns),
            matched=len(records),
            matched_rate=round(matched_rate, 4),
        )
        parts.append(group_wallet_txn_records(records))

    groups = _merge_groups(*parts)
    candidates = extract_candidates(
        groups,
        min_occurrence=min_occurrence,
        consistency_threshold=consistency_threshold,
    )
    log.info(
        "journal_rules.analyze.candidates",
        count=len(candidates),
        income=sum(1 for c in candidates if c.entry_side_str == "income"),
        expense=sum(1 for c in candidates if c.entry_side_str == "expense"),
    )
    return candidates
