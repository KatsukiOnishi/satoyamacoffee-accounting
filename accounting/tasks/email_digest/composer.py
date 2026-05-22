"""Jinja2 HTML テンプレートからダイジェスト本文を組み立てる。"""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from accounting.tasks.email_digest.models import WeeklyDigest

SUBJECT_PREFIX = "[さとやま経理]"


def render_subject(digest: WeeklyDigest) -> str:
    """件名生成。

    例: '[さとやま経理] 週次レポート 2026-W21（5/18-5/24）成功5件/要確認3件'
    """
    start = digest.week_start
    end = digest.week_end
    return (
        f"{SUBJECT_PREFIX} 週次レポート {digest.iso_week}"
        f"（{start.month}/{start.day}-{end.month}/{end.day}）"
        f"成功{digest.total_success}件/要確認{digest.total_review_required}件"
    )


def _template_env() -> Environment:
    tpl_dir = Path(__file__).resolve().parent / "templates"
    return Environment(
        loader=FileSystemLoader(str(tpl_dir)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_html(digest: WeeklyDigest, *, subject: str | None = None) -> str:
    """HTML 本文を返す。"""
    env = _template_env()
    tpl = env.get_template("digest.html.j2")
    return tpl.render(
        digest=digest,
        subject=subject or render_subject(digest),
    )


def body_summary(digest: WeeklyDigest) -> str:
    """notification_log.body_summary 用の短い文字列。"""
    if digest.mode == "production":
        return (
            f"ar_reconciled={len(digest.ar_reconciled)} "
            f"unmatched={len(digest.ar_unmatched)} "
            f"multiple={len(digest.ar_multiple_matches)} "
            f"registered={len(digest.classify_registered)} "
            f"review={len(digest.classify_review_required)} "
            f"skipped={len(digest.classify_skipped)} "
            f"failed={len(digest.classify_failed)}"
        )
    return (
        f"ar_reconciled={len(digest.ar_reconciled)} "
        f"unmatched={len(digest.ar_unmatched)} "
        f"multiple={len(digest.ar_multiple_matches)} "
        f"shadow_logged={len(digest.classify_shadow_logged)} "
        f"failed={len(digest.classify_failed)}"
    )
