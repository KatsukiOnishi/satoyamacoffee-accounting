"""shifts（attendance-system）への書き込みコネクタ。

責務: HRMOS から取得した勤怠 CSV を multipart で shifts.satoyamacoffee.com に POST し、
attendance-system 側で `lib/attendanceImport.upsertAttendanceRows` まで実行させる。

エンドポイント:
  POST {SHIFTS_BASE_URL}/api/admin/import-hrmos
  Headers: Authorization: Bearer <SHIFTS_ADMIN_API_KEY>
  Body: multipart/form-data, files[] に複数の CSV bytes

レスポンス:
  {
    "received": int,        // 受信ファイル数
    "parsedRows": int,      // パース成功 + 重複排除後の行数
    "saved": int,           // DB upsert 成功数
    "skipped": [str, ...],  // マスタ未登録などのスキップメモ
    "errors": [str, ...]    // CSV パース時の警告
  }
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from accounting.config import settings
from accounting.core.dry_run import is_dry_run
from accounting.core.logger import get_logger

logger = get_logger("shifts")


@dataclass
class ShiftsImportResponse:
    received: int
    parsed_rows: int
    saved: int
    skipped: list[str]
    errors: list[str]

    @property
    def has_warnings(self) -> bool:
        return bool(self.skipped) or bool(self.errors)


class ShiftsAdminClient:
    """attendance-system の管理者書き込み API クライアント。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = (base_url or settings.shifts_base_url).rstrip("/")
        self.api_key = api_key or settings.shifts_admin_api_key
        if not self.base_url:
            raise ValueError("SHIFTS_BASE_URL が未設定です")
        if not self.api_key:
            raise ValueError("SHIFTS_ADMIN_API_KEY が未設定です")
        self._client = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

    def __enter__(self) -> "ShiftsAdminClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def import_hrmos_csvs(
        self,
        files: list[tuple[str, bytes]],
    ) -> ShiftsImportResponse:
        """multipart で files[] を POST。dry-run のときは送信せず空レスポンスを返す。

        Args:
            files: `[(filename, csv_bytes), ...]`
        """
        if not files:
            raise ValueError("files が空です")

        if is_dry_run():
            logger.info(
                "shifts_import_dry_run",
                file_count=len(files),
                total_bytes=sum(len(b) for _, b in files),
                filenames=[name for name, _ in files],
            )
            return ShiftsImportResponse(
                received=len(files),
                parsed_rows=0,
                saved=0,
                skipped=[],
                errors=[],
            )

        url = f"{self.base_url}/api/admin/import-hrmos"
        # httpx の files= はリスト渡しで同一フィールド名複数 OK
        multipart = [
            ("files[]", (name, content, "text/csv"))
            for name, content in files
        ]
        resp = self._client.post(url, files=multipart)
        if resp.status_code != 200:
            raise RuntimeError(
                f"shifts import 失敗: status={resp.status_code} body={resp.text[:500]}"
            )
        data: dict[str, Any] = resp.json()
        result = ShiftsImportResponse(
            received=int(data.get("received", 0)),
            parsed_rows=int(data.get("parsedRows", 0)),
            saved=int(data.get("saved", 0)),
            skipped=list(data.get("skipped") or []),
            errors=list(data.get("errors") or []),
        )
        logger.info(
            "shifts_import_done",
            received=result.received,
            parsed_rows=result.parsed_rows,
            saved=result.saved,
            skipped_count=len(result.skipped),
            errors_count=len(result.errors),
        )
        return result
