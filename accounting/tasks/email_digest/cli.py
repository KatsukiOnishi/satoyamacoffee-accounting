"""email-digest CLI（typer サブアプリ）: send。"""
from __future__ import annotations

from datetime import datetime

import typer

from accounting.config import settings
from accounting.core import auto_keiri
from accounting.core.db import init_db
from accounting.core.logger import bind_run, get_logger, unbind_run
from accounting.core.report import generate_run_id
from accounting.tasks.email_digest import aggregator, composer, sender

email_digest_app = typer.Typer(help="auto-keiri 週次ダイジェストメール送信")
log = get_logger("email_digest")

TASK_NAME = "email_digest"


@email_digest_app.command("send")
def send(
    week: str = typer.Option(
        None,
        "--week",
        help="ISO 週指定（例: 2026-W21）。省略時は今週（月曜〜日曜）",
    ),
    dry_run: bool = typer.Option(
        settings.dry_run,
        "--dry-run/--no-dry-run",
        help="dry-run（既定）。本送信は --no-dry-run を明示",
    ),
    print_html: bool = typer.Option(
        False,
        "--print-html/--no-print-html",
        help="HTML 本文を標準出力にも書き出す（dry-run 用プレビュー）",
    ),
) -> None:
    """過去 7 日（または指定週）の auto-keiri 結果を1通のメールにまとめて送る。"""
    init_db()
    run_id = generate_run_id(TASK_NAME)
    bind_run(TASK_NAME, run_id)

    try:
        if week:
            start, end = aggregator.week_range_from_iso(week)
        else:
            start, end = aggregator.week_range_for()
        typer.echo(f"run_id: {run_id}  dry_run={dry_run}  week={start} - {end}")

        digest = aggregator.aggregate(start, end)
        subject = composer.render_subject(digest)
        html = composer.render_html(digest, subject=subject)

        if print_html or dry_run:
            typer.echo("---- subject ----")
            typer.echo(subject)
            typer.echo("---- html ----")
            typer.echo(html)
            typer.echo("---- end ----")

        result = sender.send_digest(
            subject=subject, html=html, dry_run=dry_run
        )

        auto_keiri.insert_notification_log(
            task=TASK_NAME,
            sent_at=datetime.utcnow(),
            week_start=start,
            week_end=end,
            recipient=result.recipient,
            subject=subject,
            body_summary=composer.body_summary(digest),
            resend_message_id=result.resend_message_id,
            success=bool(result.success),
        )

        if not result.success:
            typer.echo(f"送信失敗: {result.error}", err=True)
            raise typer.Exit(code=1)
        typer.echo(
            f"✓ digest sent: subject={subject!r}  recipient={result.recipient}  "
            f"message_id={result.resend_message_id or '(dry_run)'}"
        )
    finally:
        unbind_run()


__all__ = ["email_digest_app"]
