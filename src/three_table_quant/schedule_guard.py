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
    if market_date is None:
        current = now or datetime.now(timezone)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone)
        resolved_date = current.astimezone(timezone).date()
    else:
        resolved_date = parse_calendar_date(market_date, "market_date")

    calendar_path = config["input_contract"]["trading_calendar_path"]
    calendar = load_trading_calendar(calendar_path)
    return {
        "market_date": resolved_date.strftime("%Y%m%d"),
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
    print(f"is_open={'true' if context['is_open'] else 'false'}")
    print(f"timezone={context['timezone']}")


if __name__ == "__main__":
    main()
