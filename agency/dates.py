"""Account-timezone date windows, complete-day defaults, and conversion-lag metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


CONVERSION_LAG_WARNING_DAYS = 3


@dataclass(frozen=True)
class DateWindow:
    start: date
    end: date
    time_zone: str
    time_zone_fallback: bool
    complete_days_only: bool
    includes_incomplete_today: bool
    conversion_lag_warning: str | None

    @property
    def gaql_clause(self) -> str:
        return f"segments.date BETWEEN '{self.start.isoformat()}' AND '{self.end.isoformat()}'"

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "days": (self.end - self.start).days + 1,
            "complete_days_only": self.complete_days_only,
            "includes_incomplete_today": self.includes_incomplete_today,
        }


def account_today(
    time_zone: str | None,
    *,
    now_utc: datetime | None = None,
) -> tuple[date, str, bool]:
    """Return `(today_in_account_tz, effective_tz_name, used_fallback)`."""
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    tz_name = (time_zone or "").strip()
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
            return now.astimezone(tz).date(), tz_name, False
        except ZoneInfoNotFoundError:
            return now.astimezone(timezone.utc).date(), "UTC", True
    return now.astimezone(timezone.utc).date(), "UTC", False


def _lag_warning(end_date: date, today_local: date) -> str | None:
    days_ago = (today_local - end_date).days
    if days_ago <= CONVERSION_LAG_WARNING_DAYS:
        return (
            "Date range ends within the last 3 days of the account time zone. "
            "Recent conversion counts and values are provisional due to conversion lag; "
            "check `conversions_by_conversion_date` or allow 3–7 days for stabilization."
        )
    return None


def resolve_date_range(
    days: int = 30,
    *,
    time_zone: str | None = None,
    complete_days_only: bool = True,
    start_date: str | None = None,
    end_date: str | None = None,
    now_utc: datetime | None = None,
) -> DateWindow:
    """Build a validated DateWindow aligned to the account's time zone."""
    today_local, effective_tz, used_fallback = account_today(time_zone, now_utc=now_utc)
    if start_date or end_date:
        if not (start_date and end_date):
            raise ValueError("Both start_date and end_date must be provided together (YYYY-MM-DD)")
        start = date.fromisoformat(start_date.strip())
        end = date.fromisoformat(end_date.strip())
        if start > end or (end - start).days >= 365:
            raise ValueError("Invalid explicit date range (must be <= 365 days and start <= end)")
        includes_today = end >= today_local
        return DateWindow(
            start=start,
            end=end,
            time_zone=effective_tz,
            time_zone_fallback=used_fallback,
            complete_days_only=not includes_today,
            includes_incomplete_today=includes_today,
            conversion_lag_warning=_lag_warning(end, today_local),
        )

    if not 1 <= days <= 365:
        raise ValueError("days must be between 1 and 365")
    end = today_local - timedelta(days=1) if complete_days_only else today_local
    start = end - timedelta(days=days - 1)
    includes_today = not complete_days_only
    return DateWindow(
        start=start,
        end=end,
        time_zone=effective_tz,
        time_zone_fallback=used_fallback,
        complete_days_only=complete_days_only,
        includes_incomplete_today=includes_today,
        conversion_lag_warning=_lag_warning(end, today_local),
    )


def coverage_metadata(
    *,
    currency_code: str,
    window: DateWindow,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Standard coverage header attached to reports and time-series responses."""
    queried_at = (now_utc or datetime.now(timezone.utc)).isoformat()
    warnings: list[str] = []
    if window.includes_incomplete_today:
        warnings.append(
            "Date range includes the current incomplete day in the account time zone."
        )
    if window.conversion_lag_warning:
        warnings.append(window.conversion_lag_warning)
    if window.time_zone_fallback:
        warnings.append(
            "Account time zone could not be resolved locally; fell back to UTC."
        )
    return {
        "currency_code": currency_code,
        "time_zone": window.time_zone,
        "date_range": window.to_dict(),
        "queried_at_utc": queried_at,
        "complete_days_only": window.complete_days_only,
        "conversion_lag_warning": window.conversion_lag_warning,
        "coverage_warnings": warnings,
    }
