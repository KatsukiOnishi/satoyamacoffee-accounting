"""vendor-invoice CLI（scan / list / apply / reconcile）。

`accounting/cli.py` から `vendor_invoice_app` をマウントする。
"""
from __future__ import annotations

from datetime import date

import typer
from rich.console import Console
from rich.table import Table

from accounting.config import settings
from accounting.connectors.freee import FreeeClient
from accounting.connectors.gmail import GmailAttachment, GmailMessage
from accounting.core.db import init_db
from accounting.core.dry_run import DryRunContext, is_dry_run
from accounting.core.idempotency import is_executed
from accounting.core.logger import bind_run, get_logger, unbind_run
from accounting.core.notifier import notify_failure
from accounting.core.report import RunReport, generate_run_id
from accounting.core.vendor_invoice_candidates import (
    VendorInvoiceCandidate,
    get_by_id,
    list_by_status,
    upsert_candidate,
)
from accounting.tasks.vendor_invoice import (
    account_resolver,
    bank_detector,
    classifier as classifier_mod,
    extractor,
    fetcher,
    notifier as vi_notifier,
    partner_matcher,
    reconciler,
    registrar,
)
from accounting.tasks.vendor_invoice.blacklists import get_known_vendor

vendor_invoice_app = typer.Typer(help="ベンダー請求書の取込・freee登録・消し込み")
console = Console()
log = get_logger("vendor_invoice")

TASK_NAME = "vendor_invoice"


def _resolve_message_candidate(
    message: GmailMessage,
    freee: FreeeClient | None,
) -> list[VendorInvoiceCandidate]:
    """1メッセージを処理して候補レコードを 1 件以上作成する。"""
    verdict = classifier_mod.classify_initial(message)
    log.info(
        "vendor_invoice.classify",
        message_id=message.message_id,
        sender=message.sender,
        verdict=verdict.classification,
        reason=verdict.exclusion_reason,
    )

    # 除外メールは1レコードに集約（添付ファイルは触らない）
    if verdict.classification == "excluded":
        c = upsert_candidate(
            gmail_message_id=message.message_id,
            gmail_attachment_id="",
            received_at=message.received_at.replace(tzinfo=None),
            sender=message.sender,
            subject=message.subject,
            classification=verdict.classification,
            exclusion_reason=verdict.exclusion_reason,
            status="excluded",
        )
        return [c]

    # 本文のみメール: 本文抽出を試みる
    if verdict.classification == "no_attachment":
        return [_process_body_only(message, freee)]

    # 通常: 添付ごとに処理
    out: list[VendorInvoiceCandidate] = []
    if not message.attachments:
        # 件名は invoice キーワードがあったが添付ゼロ → manual_review
        c = upsert_candidate(
            gmail_message_id=message.message_id,
            gmail_attachment_id="",
            received_at=message.received_at.replace(tzinfo=None),
            sender=message.sender,
            subject=message.subject,
            classification="needs_review",
            status="manual_review",
            error_message="no_attachment_but_invoice_keyword",
        )
        return [c]

    for att in message.attachments:
        out.append(_process_attachment(message, att, freee))
    return out


def _process_body_only(
    message: GmailMessage, freee: FreeeClient | None
) -> VendorInvoiceCandidate:
    """添付なしメール: 本文から抽出してみる。"""
    try:
        extracted = extractor.extract_from_body(
            subject=message.subject,
            body_text=message.body_text,
            sender=message.sender,
        )
    except Exception as e:
        log.error("vendor_invoice.body_extract_failed", error=str(e))
        return upsert_candidate(
            gmail_message_id=message.message_id,
            gmail_attachment_id="",
            received_at=message.received_at.replace(tzinfo=None),
            sender=message.sender,
            subject=message.subject,
            classification="needs_review",
            status="manual_review",
            error_message=f"body_extract_failed: {e}",
        )

    return _persist_extraction(
        message=message,
        attachment_id="",
        raw_pdf_path=None,
        extracted=extracted,
        freee=freee,
    )


def _process_attachment(
    message: GmailMessage,
    attachment: GmailAttachment,
    freee: FreeeClient | None,
) -> VendorInvoiceCandidate:
    """1 添付を分類・抽出する。"""
    name_lower = attachment.filename.lower()

    # ZIPはダウンロードして暗号化判定だけ行う
    if name_lower.endswith(".zip") or "zip" in (attachment.mime_type or ""):
        try:
            saved = fetcher.download_attachment_if_pdf_or_zip(message, attachment)
        except Exception as e:
            log.error("vendor_invoice.zip_download_failed", error=str(e))
            return upsert_candidate(
                gmail_message_id=message.message_id,
                gmail_attachment_id=attachment.attachment_id,
                received_at=message.received_at.replace(tzinfo=None),
                sender=message.sender,
                subject=message.subject,
                classification="needs_review",
                status="manual_review",
                error_message=f"zip_download_failed: {e}",
            )
        encrypted = saved is not None and classifier_mod.is_encrypted_zip(saved)
        if encrypted:
            return upsert_candidate(
                gmail_message_id=message.message_id,
                gmail_attachment_id=attachment.attachment_id,
                received_at=message.received_at.replace(tzinfo=None),
                sender=message.sender,
                subject=message.subject,
                classification="encrypted_zip",
                status="manual_review",
                raw_pdf_path=str(saved) if saved else None,
                error_message="zip_is_password_protected",
            )
        # 暗号化なしZIPでも自動展開は対象外（人手レビュー）
        return upsert_candidate(
            gmail_message_id=message.message_id,
            gmail_attachment_id=attachment.attachment_id,
            received_at=message.received_at.replace(tzinfo=None),
            sender=message.sender,
            subject=message.subject,
            classification="needs_review",
            status="manual_review",
            raw_pdf_path=str(saved) if saved else None,
            error_message="non_encrypted_zip_needs_manual_unpack",
        )

    # PDF 以外はサポートせず manual_review
    if not (name_lower.endswith(".pdf") or attachment.mime_type == "application/pdf"):
        return upsert_candidate(
            gmail_message_id=message.message_id,
            gmail_attachment_id=attachment.attachment_id,
            received_at=message.received_at.replace(tzinfo=None),
            sender=message.sender,
            subject=message.subject,
            classification="needs_review",
            status="manual_review",
            error_message=f"unsupported_attachment_type: {attachment.mime_type}",
        )

    # PDF: DL → Vision
    try:
        saved = fetcher.download_attachment_if_pdf_or_zip(message, attachment)
    except Exception as e:
        return upsert_candidate(
            gmail_message_id=message.message_id,
            gmail_attachment_id=attachment.attachment_id,
            received_at=message.received_at.replace(tzinfo=None),
            sender=message.sender,
            subject=message.subject,
            classification="needs_review",
            status="manual_review",
            error_message=f"pdf_download_failed: {e}",
        )
    if saved is None:
        return upsert_candidate(
            gmail_message_id=message.message_id,
            gmail_attachment_id=attachment.attachment_id,
            received_at=message.received_at.replace(tzinfo=None),
            sender=message.sender,
            subject=message.subject,
            classification="needs_review",
            status="manual_review",
            error_message="pdf_not_saved",
        )

    try:
        extracted = extractor.extract_from_pdf(saved)
    except Exception as e:
        log.error("vendor_invoice.pdf_extract_failed", error=str(e), path=str(saved))
        return upsert_candidate(
            gmail_message_id=message.message_id,
            gmail_attachment_id=attachment.attachment_id,
            received_at=message.received_at.replace(tzinfo=None),
            sender=message.sender,
            subject=message.subject,
            classification="needs_review",
            status="manual_review",
            raw_pdf_path=str(saved),
            error_message=f"vision_extract_failed: {e}",
        )

    return _persist_extraction(
        message=message,
        attachment_id=attachment.attachment_id,
        raw_pdf_path=str(saved),
        extracted=extracted,
        freee=freee,
    )


def _persist_extraction(
    *,
    message: GmailMessage,
    attachment_id: str,
    raw_pdf_path: str | None,
    extracted,
    freee: FreeeClient | None,
) -> VendorInvoiceCandidate:
    verdict = bank_detector.reclassify_from_extraction(extracted)
    # 抽出値の正規化
    common = dict(
        gmail_message_id=message.message_id,
        gmail_attachment_id=attachment_id,
        received_at=message.received_at.replace(tzinfo=None),
        sender=message.sender,
        subject=message.subject,
        raw_pdf_path=raw_pdf_path,
        extracted_amount=extracted.total_amount,
        extracted_tax=extracted.tax_amount,
        extracted_issue_date=extracted.issue_date,
        extracted_due_date=extracted.due_date,
        extracted_partner_name=extracted.partner_name,
        extracted_bank_account=extracted.bank_account_info,
        extracted_summary=extracted.line_items_summary,
        classification=verdict.classification,
        exclusion_reason=verdict.exclusion_reason,
    )

    if verdict.classification == "excluded":
        return upsert_candidate(**common, status="excluded")

    # 抽出結果が confirmed bank_transfer_invoice → partner / account を引き当て
    if freee is None:
        # オフライン処理（freeeに繋がない）— あとで apply コマンドで処理
        return upsert_candidate(
            **common,
            status="pending",
        )

    pm = partner_matcher.match_partner(
        freee,
        extracted.partner_name,
        fallback_hint=(get_known_vendor(message.sender) or {}).get("partner_name_hint"),
    )
    if pm.partner_id is None:
        return upsert_candidate(
            **common,
            status="manual_review",
            error_message=f"partner_not_found:{pm.notes}",
        )

    hint = get_known_vendor(message.sender) or {}
    resolution = account_resolver.resolve_account_item(
        freee,
        partner_id=pm.partner_id,
        default_hint_name=hint.get("default_account_item"),
    )
    if resolution.account_item_id is None:
        return upsert_candidate(
            **common,
            freee_partner_id=pm.partner_id,
            status="manual_review",
            error_message=f"account_item_not_resolved:{resolution.notes}",
        )

    return upsert_candidate(
        **common,
        freee_partner_id=pm.partner_id,
        freee_account_item_id=resolution.account_item_id,
        freee_account_item_name=resolution.account_item_name,
        status="pending",
    )


def _register_candidate(
    freee: FreeeClient, candidate: VendorInvoiceCandidate, run_id: str
) -> str:
    """候補1件を freee に登録 → reconcile 試行。

    Returns: 新しい status
    """
    if candidate.classification != "bank_transfer_invoice":
        return candidate.status
    if (
        candidate.freee_partner_id is None
        or candidate.freee_account_item_id is None
        or candidate.extracted_amount is None
    ):
        return candidate.status

    company_id = int(settings.freee_company_id or 0)
    if not company_id:
        raise RuntimeError("FREEE_COMPANY_ID 未設定")

    external_id = registrar.build_external_id(
        candidate.gmail_message_id, candidate.gmail_attachment_id or None
    )
    if is_executed(TASK_NAME, external_id):
        return candidate.status

    payload = registrar.build_deal_payload(
        company_id=company_id,
        partner_id=candidate.freee_partner_id,
        issue_date=candidate.extracted_issue_date or date.today(),
        due_date=candidate.extracted_due_date,
        total_amount=candidate.extracted_amount,
        expense_account_item_id=candidate.freee_account_item_id,
        tax_code=account_resolver.get_unfiled_tax_code(freee),
        description=(
            f"{candidate.extracted_partner_name or candidate.sender} "
            f"{candidate.extracted_summary or ''}"
        ).strip(),
    )

    result = registrar.register_deal(freee, payload, external_id, run_id)
    if result.get("dry_run"):
        return "pending"
    deal_id = result.get("deal_id")
    new_status = "registered"
    from accounting.core.vendor_invoice_candidates import update_status as _us

    _us(candidate.id, new_status, freee_deal_id=int(deal_id) if deal_id else None)

    # reconcile 試行（再fetch して latest 反映）
    latest = get_by_id(candidate.id)
    if latest is not None:
        reconciler.reconcile_candidate(freee, latest)
    refreshed = get_by_id(candidate.id)
    return refreshed.status if refreshed else new_status


# --------------- commands --------------- #


@vendor_invoice_app.command("scan")
def scan(
    days: int = typer.Option(30, "--days", help="過去N日分のGmailをスキャン"),
    dry_run: bool = typer.Option(
        settings.dry_run,
        "--dry-run/--no-dry-run",
        help="dry-run（既定）。本番登録は --no-dry-run を明示",
    ),
    notify: bool = typer.Option(True, "--notify/--no-notify", help="Resend メール送信"),
    max_results: int = typer.Option(500, "--max-results", help="Gmail検索の上限件数"),
) -> None:
    """Gmail を走査して候補テーブルに書き込み、freee へ登録する（dry-run可）。"""
    init_db()
    run_id = generate_run_id(TASK_NAME)
    bind_run(TASK_NAME, run_id)
    report = RunReport(task=TASK_NAME, run_id=run_id)
    typer.echo(f"run_id: {run_id}  dry_run={dry_run}  days={days}")

    candidate_ids: list[int] = []
    try:
        with DryRunContext(dry_run):
            messages = fetcher.fetch_recent_messages(days, max_results=max_results)
            log.info("vendor_invoice.fetched", count=len(messages))

            new_messages = []
            for m in messages:
                ext = registrar.build_external_id(
                    m.message_id, None  # body-only キーで雑チェック
                )
                # body-only も attachment 個別もまとめて idempotency で弾けるよう、
                # ここでは「全添付＋本文の全 external_id が is_executed」なら skip。
                # 簡易判定: attachment_id ごとに後段でチェックされるので、ここは通す。
                new_messages.append(m)
                _ = ext  # placeholder（厳密チェックは後段の registrar 側）

            freee_client: FreeeClient | None = None
            try:
                # freee が必要 (partner / account_item の引き当て) — dry-run でも読み取りはする
                freee_client = FreeeClient()
                for m in new_messages:
                    try:
                        results = _resolve_message_candidate(m, freee_client)
                        for c in results:
                            candidate_ids.append(c.id)
                            report.add_success(c.id, c.classification)
                            # 本番モードかつ pending → 登録試行
                            if (
                                not dry_run
                                and c.status == "pending"
                                and c.classification == "bank_transfer_invoice"
                            ):
                                try:
                                    new_status = _register_candidate(
                                        freee_client, c, run_id
                                    )
                                    log.info(
                                        "vendor_invoice.candidate_registered",
                                        candidate_id=c.id,
                                        new_status=new_status,
                                    )
                                except Exception as reg_err:
                                    log.exception(
                                        "vendor_invoice.register_failed",
                                        candidate_id=c.id,
                                    )
                                    from accounting.core.vendor_invoice_candidates import (
                                        update_status as _us,
                                    )

                                    _us(
                                        c.id,
                                        "failed",
                                        error_message=f"register_failed: {reg_err}",
                                    )
                                    report.add_failure(c.id, reg_err)
                    except Exception as e:
                        log.exception(
                            "vendor_invoice.message_failed",
                            message_id=m.message_id,
                        )
                        report.add_failure(m.message_id, e)
            finally:
                if freee_client is not None:
                    freee_client.close()
    except Exception as e:
        log.exception("vendor_invoice.scan_failed")
        report.add_failure("scan", e)
        notify_failure(
            TASK_NAME, run_id, e, {"days": days, "dry_run": dry_run}
        )
        raise
    finally:
        unbind_run()

    # 集計 + Resend
    from accounting.core.vendor_invoice_candidates import get_by_id as _g

    candidates: list[VendorInvoiceCandidate] = [
        c for c in (_g(cid) for cid in set(candidate_ids)) if c is not None
    ]
    _render_summary_table(candidates, dry_run)
    if notify:
        vi_notifier.notify_run_summary(
            run_id=run_id,
            scan_days=days,
            candidates=candidates,
            dry_run=dry_run,
        )
    report.finalize()


def _render_summary_table(
    candidates: list[VendorInvoiceCandidate], dry_run: bool
) -> None:
    from collections import Counter

    counter: Counter[str] = Counter(c.status for c in candidates)
    t = Table(
        title=f"vendor-invoice scan 結果 (dry_run={dry_run})",
        show_header=True,
    )
    t.add_column("status")
    t.add_column("count", justify="right")
    for status in (
        "reconciled",
        "registered",
        "unpaid",
        "pending",
        "manual_review",
        "excluded",
        "failed",
    ):
        t.add_row(status, str(counter.get(status, 0)))
    console.print(t)


@vendor_invoice_app.command("list")
def list_candidates(
    status: str = typer.Option(
        "pending,manual_review,unpaid,registered",
        "--status",
        help="カンマ区切りのstatusフィルタ",
    ),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """候補テーブルから状態別に一覧表示する。"""
    init_db()
    statuses = [s.strip() for s in status.split(",") if s.strip()]
    rows = list_by_status(statuses)[:limit]
    if not rows:
        typer.echo(f"({status} に該当する候補はありません)")
        raise typer.Exit(code=0)

    t = Table(
        title=f"vendor-invoice candidates ({status}) max={limit}",
        show_header=True,
    )
    t.add_column("id", justify="right")
    t.add_column("status")
    t.add_column("classification")
    t.add_column("sender")
    t.add_column("partner")
    t.add_column("amount", justify="right")
    t.add_column("due")
    t.add_column("deal_id")
    for c in rows:
        t.add_row(
            str(c.id),
            c.status,
            c.classification,
            c.sender[:30],
            (c.extracted_partner_name or "")[:24],
            f"{c.extracted_amount:,}" if c.extracted_amount else "",
            c.extracted_due_date.isoformat() if c.extracted_due_date else "",
            str(c.freee_deal_id or ""),
        )
    console.print(t)


@vendor_invoice_app.command("apply")
def apply(
    candidate_id: int = typer.Argument(..., help="候補テーブルの id"),
    dry_run: bool = typer.Option(
        settings.dry_run,
        "--dry-run/--no-dry-run",
        help="dry-run（既定）",
    ),
) -> None:
    """個別の候補を freee に登録する（manual_review / pending を対象）。"""
    init_db()
    c = get_by_id(candidate_id)
    if c is None:
        typer.echo(f"id={candidate_id} の候補が見つかりません", err=True)
        raise typer.Exit(code=1)
    if c.classification != "bank_transfer_invoice":
        typer.echo(
            f"classification={c.classification} の候補は登録対象外です（excluded など）。",
            err=True,
        )
        raise typer.Exit(code=2)

    run_id = generate_run_id(TASK_NAME)
    bind_run(TASK_NAME, run_id)
    try:
        with DryRunContext(dry_run):
            with FreeeClient() as freee:
                # 引き当てが未済なら再試行
                if c.freee_partner_id is None or c.freee_account_item_id is None:
                    pm = partner_matcher.match_partner(
                        freee,
                        c.extracted_partner_name,
                        fallback_hint=(
                            get_known_vendor(c.sender) or {}
                        ).get("partner_name_hint"),
                    )
                    if pm.partner_id is None:
                        typer.echo("partner が引き当てられません。freee 側で登録してください。", err=True)
                        raise typer.Exit(code=3)
                    hint = get_known_vendor(c.sender) or {}
                    res = account_resolver.resolve_account_item(
                        freee,
                        partner_id=pm.partner_id,
                        default_hint_name=hint.get("default_account_item"),
                    )
                    if res.account_item_id is None:
                        typer.echo("account_item が引き当てられません。", err=True)
                        raise typer.Exit(code=4)
                    from accounting.core.vendor_invoice_candidates import (
                        update_status as _us,
                    )

                    _us(
                        c.id,
                        "pending",
                        freee_partner_id=pm.partner_id,
                        freee_account_item_id=res.account_item_id,
                        freee_account_item_name=res.account_item_name,
                    )
                    c = get_by_id(candidate_id) or c

                new_status = _register_candidate(freee, c, run_id)
                typer.echo(f"✓ candidate_id={candidate_id} status={new_status}")
                if is_dry_run():
                    typer.echo("[dry-run] freee には何も書き込んでいません。")
    finally:
        unbind_run()


@vendor_invoice_app.command("reconcile")
def reconcile(
    dry_run: bool = typer.Option(
        settings.dry_run,
        "--dry-run/--no-dry-run",
        help="dry-run（既定）",
    ),
) -> None:
    """登録済み未払を walk して、振込が降りていれば消し込み（status='reconciled'）に更新する。"""
    init_db()
    run_id = generate_run_id(TASK_NAME)
    bind_run(TASK_NAME, run_id)
    try:
        with DryRunContext(dry_run):
            results = reconciler.reconcile_pending()
    finally:
        unbind_run()

    matched = [r for r in results if r.matched]
    typer.echo(f"消し込み成功: {len(matched)} / 試行: {len(results)}")
    for r in matched:
        typer.echo(f"  candidate_id={r.candidate_id} → deal_id={r.matched_deal_id}")


# 互換用のエクスポート（CLI ファイル単体実行や型ヒント目的）
__all__ = ["vendor_invoice_app"]
