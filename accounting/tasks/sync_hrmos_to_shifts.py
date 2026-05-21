"""sync-hrmos タスク。

HRMOS から指定月の社員別勤怠 CSV を取得し、shifts.satoyamacoffee.com の
管理者 API (POST /api/admin/import-hrmos) に multipart で一括投入する。

呼び出し: `accounting sync-hrmos --month YYYY-MM [--no-dry-run] [--user-ids 7,8]`

冪等性:
- 全件モード（--user-ids 未指定）の場合、`(task='sync-hrmos', external_id=YYYY-MM)` で
  is_executed をチェックする。再実行したい場合は --user-ids で個別指定する運用にする。
- shifts 側は (staffId, date) で upsert するため、CSV を再送しても二重登録にはならない。
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Any

from accounting.config import settings
from accounting.connectors.hrmos import HrmosClient, HrmosCsv
from accounting.connectors.shifts import ShiftsAdminClient
from accounting.core import idempotency, notifier
from accounting.core.db import init_db
from accounting.core.dry_run import DryRunContext, is_dry_run
from accounting.core.logger import bind_run, get_logger, unbind_run
from accounting.core.report import new_report


def previous_month_yyyymm(today: date | None = None) -> str:
    """実行日基準で前月を YYYY-MM で返す。"""
    today = today or date.today()
    first_of_this_month = today.replace(day=1)
    last_of_prev_month = first_of_this_month.replace(
        year=first_of_this_month.year - 1 if first_of_this_month.month == 1 else first_of_this_month.year,
        month=12 if first_of_this_month.month == 1 else first_of_this_month.month - 1,
        day=1,
    )
    return last_of_prev_month.strftime("%Y-%m")


def run(
    month: str | None = None,
    dry_run: bool = True,
    user_ids: list[int] | None = None,
) -> dict[str, Any]:
    init_db()
    yyyymm = month or previous_month_yyyymm()
    if not _is_valid_yyyymm(yyyymm):
        raise ValueError(f"month は YYYY-MM 形式で指定してください: {yyyymm!r}")

    report = new_report("sync-hrmos")
    run_id = report.run_id
    bind_run("sync-hrmos", run_id)
    logger = get_logger("sync-hrmos")

    try:
        with DryRunContext(dry_run):
            logger.info(
                "sync_hrmos.start",
                month=yyyymm,
                dry_run=is_dry_run(),
                user_ids=user_ids,
            )

            external_id = yyyymm
            full_run = user_ids is None
            if full_run and idempotency.is_executed("sync-hrmos", external_id):
                logger.info("sync_hrmos.skip_duplicate", external_id=external_id)
                report.add_warning(
                    external_id,
                    "already executed; 再実行は --user-ids で個別指定するか冪等性レコードを消す",
                )
                return _finalize(report, shifts_result=None)

            csvs = _download_all(yyyymm, user_ids, report, logger)
            if not csvs:
                report.add_warning(yyyymm, "1件もCSVを取得できなかった")
                return _finalize(report, shifts_result=None)

            with ShiftsAdminClient() as shifts:
                shifts_result = shifts.import_hrmos_csvs(
                    files=[(c.filename, c.content) for c in csvs],
                )
            logger.info(
                "sync_hrmos.shifts_response",
                **asdict(shifts_result),
            )
            for s in shifts_result.skipped:
                report.add_warning("shifts_skipped", s)
            for e in shifts_result.errors:
                report.add_warning("shifts_errors", e)

            if full_run:
                idempotency.mark_executed(
                    task="sync-hrmos",
                    external_id=external_id,
                    run_id=run_id,
                    freee_journal_id=None,
                    status="dry_run" if is_dry_run() else "success",
                )

            return _finalize(report, shifts_result=shifts_result)
    except Exception as e:
        logger.error("sync_hrmos.failed", error=str(e))
        notifier.notify_failure(
            "sync-hrmos",
            run_id,
            e,
            {"month": yyyymm, "dry_run": dry_run, "user_ids": user_ids},
        )
        raise
    finally:
        unbind_run()


def _download_all(
    yyyymm: str,
    user_ids: list[int] | None,
    report,
    logger,
) -> list[HrmosCsv]:
    """全社員（または指定された user_id）の CSV をダウンロードする。

    user_ids 未指定なら `/staffs` から在籍中の全員を取り、HRMOS_EXCLUDE_USER_IDS で
    指定された ID（テストアカウント・退職者）を除外したリストを対象にする。
    """
    csvs: list[HrmosCsv] = []
    exclude = settings.hrmos_exclude_user_id_set
    with HrmosClient() as hrmos:
        hrmos.login()
        if user_ids:
            target_ids = user_ids
        else:
            staffs = hrmos.list_active_staffs()
            target_ids = [s.user_id for s in staffs if s.user_id not in exclude]
            excluded = [s for s in staffs if s.user_id in exclude]
            if excluded:
                logger.info(
                    "hrmos_excluded",
                    excluded=[{"user_id": s.user_id, "name": s.name} for s in excluded],
                )
                for s in excluded:
                    report.add_warning(s.user_id, f"excluded by HRMOS_EXCLUDE_USER_IDS ({s.name})")
        if not target_ids:
            return csvs
        for uid in target_ids:
            try:
                csv = hrmos.download_csv(yyyymm, uid)
                csvs.append(csv)
                report.add_success(uid, f"{len(csv.content)} bytes")
            except RuntimeError as e:
                # 「空」エラー = その月に勤怠データが無い社員（保坂さんが 2026-04 に
                # 勤怠ゼロだったケースなど）。失敗ではなく warning に降格してログ続行。
                if "空" in str(e):
                    logger.info("hrmos_csv_empty", user_id=uid)
                    report.add_warning(uid, "対象月に勤怠データなし")
                else:
                    logger.warning("hrmos_csv_failed", user_id=uid, error=str(e))
                    report.add_failure(uid, e)
            except Exception as e:
                logger.warning("hrmos_csv_failed", user_id=uid, error=str(e))
                report.add_failure(uid, e)
    return csvs


def _finalize(report, shifts_result) -> dict[str, Any]:
    summary = report.finalize()
    if shifts_result is not None:
        summary["shifts_result"] = asdict(shifts_result)
    return {"run_id": report.run_id, "summary": summary}


def _is_valid_yyyymm(s: str) -> bool:
    if len(s) != 7 or s[4] != "-":
        return False
    try:
        year, month = int(s[:4]), int(s[5:])
    except ValueError:
        return False
    return 1900 <= year <= 9999 and 1 <= month <= 12
