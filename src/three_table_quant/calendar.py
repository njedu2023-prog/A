from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .domain import ContractError


DEFAULT_CALENDAR_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "trading_calendar_2026.json"
)


def parse_calendar_date(value: Any, field_name: str) -> date:
    raw = str(value or "").strip()
    if len(raw) != 8 or not raw.isdigit():
        raise ContractError(f"{field_name} must be YYYYMMDD, got {value!r}")
    try:
        parsed = datetime.strptime(raw, "%Y%m%d").date()
    except ValueError as exc:
        raise ContractError(f"{field_name} is not a valid date: {value!r}") from exc
    if parsed.strftime("%Y%m%d") != raw:
        raise ContractError(f"{field_name} is not canonical YYYYMMDD: {value!r}")
    return parsed


@dataclass(frozen=True)
class TradingCalendar:
    market: str
    year: int
    weekend_days: frozenset[int]
    closed_dates: frozenset[date]
    source_url: str
    source_published: str

    @classmethod
    def from_file(cls, path: str | Path = DEFAULT_CALENDAR_PATH) -> "TradingCalendar":
        calendar_path = Path(path)
        try:
            with calendar_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError(f"cannot load trading calendar {calendar_path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ContractError("trading calendar payload must be an object")
        if payload.get("schema_version") != "sse_trading_calendar_v1":
            raise ContractError(
                f"unsupported trading calendar schema: {payload.get('schema_version')!r}"
            )
        try:
            year = int(payload["year"])
            weekend_days = frozenset(int(item) for item in payload["weekend_days"])
            closed_dates = frozenset(
                parse_calendar_date(str(item).replace("-", ""), "closed_date")
                for item in payload["closed_dates"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError(f"invalid trading calendar payload: {exc}") from exc
        if weekend_days != frozenset({5, 6}):
            raise ContractError("SSE calendar weekend_days must be [5, 6]")
        if any(item.year != year for item in closed_dates):
            raise ContractError("trading calendar contains a closed date outside its year")
        source_url = str(payload.get("source_url") or "").strip()
        source_published = str(payload.get("source_published") or "").strip()
        if not source_url.startswith("https://www.sse.com.cn/") or not source_published:
            raise ContractError("trading calendar must identify the official SSE source")
        return cls(
            market=str(payload.get("market") or "SSE"),
            year=year,
            weekend_days=weekend_days,
            closed_dates=closed_dates,
            source_url=source_url,
            source_published=source_published,
        )

    def _require_supported_year(self, value: date, field_name: str) -> None:
        if value.year != self.year:
            raise ContractError(
                f"{field_name}={value:%Y%m%d} is outside supported calendar year {self.year}"
            )

    def is_open(self, value: date, field_name: str = "date") -> bool:
        self._require_supported_year(value, field_name)
        return value.weekday() not in self.weekend_days and value not in self.closed_dates

    def next_open_date(self, value: date, field_name: str = "date") -> date:
        self._require_supported_year(value, field_name)
        cursor = value
        for _ in range(32):
            cursor += timedelta(days=1)
            self._require_supported_year(cursor, field_name)
            if self.is_open(cursor, field_name):
                return cursor
        raise ContractError(f"no next SSE trading day found after {value:%Y%m%d}")

    def validate_d_t_t1(self, decision_date: str, buy_date: str, exit_date: str) -> None:
        d = parse_calendar_date(decision_date, "D")
        t = parse_calendar_date(buy_date, "T")
        t1 = parse_calendar_date(exit_date, "T+1")
        if not self.is_open(d, "D"):
            raise ContractError(f"D={decision_date} is not an SSE trading day")
        expected_t = self.next_open_date(d, "T")
        if t != expected_t:
            raise ContractError(
                f"T={buy_date} is not the next SSE trading day after D={decision_date}; "
                f"expected {expected_t:%Y%m%d}"
            )
        expected_t1 = self.next_open_date(t, "T+1")
        if t1 != expected_t1:
            raise ContractError(
                f"T+1={exit_date} is not the next SSE trading day after T={buy_date}; "
                f"expected {expected_t1:%Y%m%d}"
            )
