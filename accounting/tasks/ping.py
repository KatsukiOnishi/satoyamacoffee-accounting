"""共通基盤の疎通確認用ダミータスク。

logger / notifier / idempotency / report / dry_run の各モジュールが正しく動くかを確認する。
freee には触らない。
"""
from __future__ import annotations

from accounting.core import idempotency, notifier
from accounting.core.db import init_db
from accounting.core.dry_run import DryRunContext, is_dry_run
from accounting.core.logger import bind_run, get_logger, unbind_run
from accounting.core.report import new_report


def run(dry_run: bool = True) -> dict:
    init_db()
    report = new_report("ping")
    run_id = report.run_id
    bind_run("ping", run_id)
    logger = get_logger("ping")

    try:
        with DryRunContext(dry_run):
            logger.info("ping.start", dry_run=is_dry_run())

            # 共通基盤の疎通確認用に固定 external_id を使う。2回目以降は skip されるのが正しい挙動。
            external_id = "ping-sentinel"
            if idempotency.is_executed("ping", external_id):
                logger.info("ping.skip_duplicate", external_id=external_id)
                report.add_warning(external_id, "already executed")
            else:
                # ping は freee に触らないため、dry_run でも本番でも success として記録する
                idempotency.mark_executed(
                    task="ping",
                    external_id=external_id,
                    run_id=run_id,
                    freee_journal_id=None,
                    status="success",
                )
                report.add_success(external_id, "idempotency write ok")

            if not is_dry_run():
                notifier.notify_summary(
                    "ping",
                    run_id,
                    {"message": "ping task succeeded", "dry_run": False},
                )
            else:
                logger.info("ping.notifier_skipped", reason="dry_run")

            summary = report.finalize()
            logger.info("ping.done", summary=summary)
            return {"run_id": run_id, "summary": summary}
    except Exception as e:
        logger.error("ping.failed", error=str(e))
        notifier.notify_failure("ping", run_id, e, {"dry_run": dry_run})
        raise
    finally:
        unbind_run()
