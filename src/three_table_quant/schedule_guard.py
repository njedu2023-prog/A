from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .calendar import load_trading_calendar, parse_calendar_date
from .pipeline import load_config


def market_day_context(
    config_path: str | Path = "config/system.json",
    *,
    now: datetime | None = None,
    market_date: str | None = None,
) -> dict[str, Any]:
    """Resolve today's Shanghai date against the versioned SSE calendar."""

    config = load_config(config_path)
    timezone_name = str(config.get("timezone") or "Asia/Shanghai")
    timezone = ZoneInfo(timezone_name)
    current = now or datetime.now(timezone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone)
    local_today = current.astimezone(timezone).date()
    if market_date is None:
        resolved_date = local_today
    else:
        resolved_date = parse_calendar_date(market_date, "market_date")

    if resolved_date < local_today:
        date_relation = "PAST"
    elif resolved_date > local_today:
        date_relation = "FUTURE"
    else:
        date_relation = "TODAY"

    calendar_path = config["input_contract"]["trading_calendar_path"]
    calendar = load_trading_calendar(calendar_path)
    return {
        "market_date": resolved_date.strftime("%Y%m%d"),
        "local_today": local_today.strftime("%Y%m%d"),
        "date_relation": date_relation,
        "is_open": calendar.is_open(resolved_date, "scheduled market date"),
        "timezone": timezone_name,
        "calendar_path": str(calendar_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check whether the scheduled Shanghai date is an SSE trading day"
    )
    parser.add_argument("--config", default="config/system.json")
    parser.add_argument("--date", help="Optional deterministic YYYYMMDD date")
    args = parser.parse_args()
    context = market_day_context(args.config, market_date=args.date)
    print(f"market_date={context['market_date']}")
    print(f"local_today={context['local_today']}")
    print(f"date_relation={context['date_relation']}")
    print(f"is_open={'true' if context['is_open'] else 'false'}")
    print(f"timezone={context['timezone']}")


if __name__ == "__main__":
    main()
