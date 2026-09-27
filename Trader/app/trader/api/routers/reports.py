"""Report routes (BR-61 web side; P5-T12): `GET /api/reports/weekly?week=YYYY-MM-DD` -> `WeeklyReportOut`
(404 when the week has no stored report).

P5-T1 registers this router with no routes; P5-T12 adds them.
"""

from fastapi import APIRouter

router = APIRouter(tags=["reports"])
