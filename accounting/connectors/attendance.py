"""attendance-system (勤怠・給与) コネクタ。後続タスクで実装する。

責務: 月次給与（基本給・所得税・住民税・社会保険・交通費）の取得。
"""
from __future__ import annotations

import httpx

from accounting.config import settings


class AttendanceClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = (base_url or settings.attendance_system_base_url).rstrip("/")
        self.api_key = api_key or settings.attendance_system_api_key
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()
