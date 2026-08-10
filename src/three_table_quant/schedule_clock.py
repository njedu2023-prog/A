from __future__ import annotations

import argparse
import math
import time
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .domain import ContractError


def _timezone(value: Any) -> ZoneInfo:
    name = str(value or "").strip()
    if not name:
        raise ContractError("timezone must be non-empty")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ContractError(f"unknown timezone: {name!r}") from exc


def _market_date(value: Any) -> datetime:
    raw = str(value or "").strip()
    if len(raw) != 8 or not raw.isdigit():
        raise ContractError("market_date must be YYYYMMDD")
    try:
        return datetime.strptime(raw, "%Y%m%d")
    except ValueError as exc:
        raise ContractError("market_date must be a valid YYYYMMDD date") from exc


def _not_before(value: Any) -> tuple[int, int]:
    raw = str(value or "").strip()
    try:
        parsed = datetime.strptime(raw, "%H:%M")
    except ValueError as exc:
        raise ContractError("not_before must be HH:MM") from exc
    if parsed.strftime("%H:%M") != raw:
        raise ContractError("not_before must be zero-padded HH:MM")
    return parsed.hour, parsed.minute


def _wait_budget(value: Any) -> float:
    if isinstance(value, bool):
        raise ContractError("max_wait_seconds must be a nonnegative finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(
            "max_wait_seconds must be a nonnegative finite number"
        ) from exc
    if not math.isfinite(parsed) or parsed < 0.0:
        raise ContractError("max_wait_seconds must be a nonnegative finite number")
    return parsed


def wait_until_not_before(
    timezone_name: str,
    market_date: str,
    not_before: str,
    max_wait_seconds: float,
    *,
    now: datetime | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Wait within one market date until the configured local wall clock.

    A caller on another local date, or one that would need to wait beyond its
    explicit budget, is rejected before sleeping.  This prevents a delayed
    runner from silently executing against the wrong trading day.
    """

    timezone = _timezone(timezone_name)
    parsed_date = _market_date(market_date)
    hour, minute = _not_before(not_before)
    wait_budget = _wait_budget(max_wait_seconds)
    current = now or datetime.now(timezone)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ContractError("now must include a timezone")
    local_now = current.astimezone(timezone)
    expected_date = parsed_date.date()
    if local_now.date() != expected_date:
        raise ContractError(
            "schedule clock local date does not match market_date; refusing cross-date run"
        )
    target = datetime(
        expected_date.year,
        expected_date.month,
        expected_date.day,
        hour,
        minute,
        tzinfo=timezone,
    )
    delay_seconds = max(0.0, (target - local_now).total_seconds())
    if delay_seconds > wait_budget:
        raise ContractError(
            f"required wait {delay_seconds:g}s exceeds max_wait_seconds "
            f"{wait_budget:g}s"
        )
    if delay_seconds > 0.0:
        sleeper(delay_seconds)
    return {
        "status": "READY",
        "timezone": timezone.key,
        "market_date": expected_date.strftime("%Y%m%d"),
        "not_before": f"{hour:02d}:{minute:02d}",
        "observed_at": local_now.isoformat(),
        "target_at": target.isoformat(),
        "waited_seconds": delay_seconds,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Wait until a same-day local automation wall clock"
    )
    parser.add_argument("--timezone", required=True)
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--not-before", required=True)
    parser.add_argument("--max-wait-seconds", required=True, type=float)
    args = parser.parse_args()
    result = wait_until_not_before(
        args.timezone,
        args.market_date,
        args.not_before,
        args.max_wait_seconds,
    )
    print("schedule_ready=true")
    print(f"waited_seconds={result['waited_seconds']:g}")


if __name__ == "__main__":
    main()
