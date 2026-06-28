from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .models import ReportMetadata


def enrich_report_metadata(metadata: ReportMetadata, input_path: str | Path, config: dict[str, Any]) -> ReportMetadata:
    buy_date = _resolve_date(metadata.auction_buy_date, Path(input_path))
    if buy_date is None:
        return metadata
    sell_date = next_trading_day(buy_date, config)
    return ReportMetadata(
        auction_buy_date=metadata.auction_buy_date,
        auction_buy_date_iso=buy_date.isoformat(),
        t1_sell_date=sell_date.isoformat(),
    )


def next_trading_day(day: date, config: dict[str, Any]) -> date:
    calendar = config.get("trading_calendar", {})
    holidays = set(calendar.get("holidays", []))
    make_up_days = set(calendar.get("make_up_trading_days", []))
    current = day + timedelta(days=1)
    while not _is_trading_day(current, holidays, make_up_days):
        current += timedelta(days=1)
    return current


def _is_trading_day(day: date, holidays: set[str], make_up_days: set[str]) -> bool:
    iso = day.isoformat()
    if iso in make_up_days:
        return True
    return day.weekday() < 5 and iso not in holidays


def _resolve_date(value: str, input_path: Path) -> date | None:
    if not value:
        return None
    normalized = value.strip().replace("/", "-").replace(".", "-")
    full_match = re.search(r"(?P<year>20\d{2})-(?P<month>\d{1,2})-(?P<day>\d{1,2})", normalized)
    if full_match:
        return date(int(full_match.group("year")), int(full_match.group("month")), int(full_match.group("day")))
    short_match = re.search(r"(?P<month>\d{1,2})-(?P<day>\d{1,2})", normalized)
    if not short_match:
        return None
    year = _infer_year(input_path)
    if year is None:
        return None
    return date(year, int(short_match.group("month")), int(short_match.group("day")))


def _infer_year(input_path: Path) -> int | None:
    match = re.search(r"(20\d{2})", str(input_path))
    return int(match.group(1)) if match else None
