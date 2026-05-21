"""attendance-system (勤怠・給与) コネクタ。

責務: 月次給与（基本給・所得税・住民税・社会保険・交通費）の取得。

attendance-system 側エンドポイント:
  GET /api/external/salary?year=YYYY&month=MM
  Headers: Authorization: Bearer <EXTERNAL_API_KEY>

レスポンス:
  {
    "year": 2026, "month": 5, "count": 4,
    "salaries": [
      {
        "staffId", "employeeNo", "lastName", "firstName", "department",
        "workDays", "workMin",
        "basePay", "transportPay",
        "incomeTax", "residentTax", "socialIns",
        "totalPay", "totalDeduct", "netPay"
      },
      ...
    ]
  }
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from accounting.config import settings
from accounting.core.logger import get_logger

logger = get_logger("attendance")


@dataclass
class SalaryRow:
    """1社員1月分の給与（整数円、attendance-system 側で Math.round 済み）。"""

    staff_id: str
    employee_no: str
    last_name: str
    first_name: str
    department: str | None
    work_days: int
    work_min: int
    base_pay: int
    transport_pay: int
    income_tax: int
    resident_tax: int
    social_ins: int
    total_pay: int
    total_deduct: int
    net_pay: int

    @property
    def full_name(self) -> str:
        return f"{self.last_name} {self.first_name}".strip()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SalaryRow":
        return cls(
            staff_id=str(d.get("staffId") or ""),
            employee_no=str(d.get("employeeNo") or ""),
            last_name=str(d.get("lastName") or ""),
            first_name=str(d.get("firstName") or ""),
            department=d.get("department"),
            work_days=int(d.get("workDays") or 0),
            work_min=int(d.get("workMin") or 0),
            base_pay=int(d.get("basePay") or 0),
            transport_pay=int(d.get("transportPay") or 0),
            income_tax=int(d.get("incomeTax") or 0),
            resident_tax=int(d.get("residentTax") or 0),
            social_ins=int(d.get("socialIns") or 0),
            total_pay=int(d.get("totalPay") or 0),
            total_deduct=int(d.get("totalDeduct") or 0),
            net_pay=int(d.get("netPay") or 0),
        )


class AttendanceClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not (base_url or settings.attendance_system_base_url):
            raise ValueError("ATTENDANCE_SYSTEM_BASE_URL が未設定です")
        self.base_url = (base_url or settings.attendance_system_base_url).rstrip("/")
        self.api_key = api_key or settings.attendance_system_api_key
        if not self.api_key:
            raise ValueError("ATTENDANCE_SYSTEM_API_KEY が未設定です")
        self._client = httpx.Client(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AttendanceClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def get_salaries(self, *, year: int, month: int) -> list[SalaryRow]:
        """指定月の社員別給与を取得する。

        Args:
            year: 西暦
            month: 1〜12

        Returns: SalaryRow のリスト。0件もあり得る。
        Raises: httpx.HTTPStatusError（401/400 等）
        """
        if not (1 <= month <= 12):
            raise ValueError(f"month は 1〜12 を指定してください: {month}")
        url = f"{self.base_url}/api/external/salary"
        res = self._client.get(
            url,
            headers=self._headers(),
            params={"year": year, "month": month},
        )
        if not res.is_success:
            logger.error(
                "attendance.salary.api_error",
                status=res.status_code,
                body=res.text[:500],
            )
            res.raise_for_status()
        data = res.json()
        rows = [SalaryRow.from_dict(d) for d in (data.get("salaries") or [])]
        logger.info(
            "attendance.salary.fetched",
            year=year,
            month=month,
            count=len(rows),
        )
        return rows
