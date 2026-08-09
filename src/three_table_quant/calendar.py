from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .domain import ContractError


SINGLE_YEAR_CALENDAR_SCHEMA = "sse_trading_calendar_v1"
CALENDAR_BUNDLE_SCHEMA = "sse_trading_calendar_bundle_v1"
CALENDAR_CANDIDATE_SCHEMA = "sse_trading_calendar_candidate_v1"
VERIFIED = "VERIFIED"
UNVERIFIED = "UNVERIFIED_AWAITING_OFFICIAL_NOTICE"
OFFICIAL_SSE_CALENDAR_INDEX = (
    "https://www.sse.com.cn/disclosure/dealinstruc/closed/"
)

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DEFAULT_SINGLE_YEAR_CALENDAR_PATH = _DATA_DIR / "trading_calendar_2026.json"
DEFAULT_CALENDAR_PATH = _DATA_DIR / "trading_calendar_bundle.json"


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


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractError(f"{label} payload must be an object")
    return payload


def _safe_relative_path(base: Path, value: Any, field_name: str) -> Path:
    raw = str(value or "").strip()
    relative = Path(raw)
    if not raw or relative.is_absolute() or ".." in relative.parts:
        raise ContractError(f"{field_name} must be a safe relative path")
    resolved = (base / relative).resolve()
    base_resolved = base.resolve()
    if resolved != base_resolved and base_resolved not in resolved.parents:
        raise ContractError(f"{field_name} escapes the calendar bundle directory")
    return resolved


@dataclass(frozen=True)
class TradingCalendar:
    market: str
    year: int
    weekend_days: frozenset[int]
    closed_dates: frozenset[date]
    source_url: str
    source_published: str

    @classmethod
    def from_file(
        cls,
        path: str | Path = DEFAULT_SINGLE_YEAR_CALENDAR_PATH,
    ) -> "TradingCalendar":
        calendar_path = Path(path)
        return cls.from_payload(
            _load_json(calendar_path, "trading calendar"),
            source_path=calendar_path,
        )

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        source_path: str | Path | None = None,
    ) -> "TradingCalendar":
        if payload.get("schema_version") != SINGLE_YEAR_CALENDAR_SCHEMA:
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
        if isinstance(payload.get("year"), bool) or year < 2000 or year > 9999:
            raise ContractError("trading calendar year is invalid")
        if weekend_days != frozenset({5, 6}):
            raise ContractError("SSE calendar weekend_days must be [5, 6]")
        if any(item.year != year for item in closed_dates):
            raise ContractError("trading calendar contains a closed date outside its year")
        market = str(payload.get("market") or "").strip()
        timezone = str(payload.get("timezone") or "").strip()
        if market != "SSE" or timezone != "Asia/Shanghai":
            raise ContractError(
                "trading calendar market/timezone must be SSE/Asia/Shanghai"
            )
        source_url = str(payload.get("source_url") or "").strip()
        source_published = str(payload.get("source_published") or "").strip()
        if not source_url.startswith("https://www.sse.com.cn/") or not source_published:
            location = f" in {source_path}" if source_path is not None else ""
            raise ContractError(
                f"trading calendar{location} must identify the official SSE source"
            )
        return cls(
            market=market,
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
        _validate_d_t_t1(self, decision_date, buy_date, exit_date)


@dataclass(frozen=True)
class CalendarBundleEntry:
    year: int
    status: str
    path: str


def validate_unverified_calendar_candidate(
    payload: Any,
    *,
    expected_year: int | None = None,
) -> dict[str, Any]:
    """Validate metadata only; candidate dates are never executable.

    An unverified candidate deliberately contains no inferred open or closed
    dates.  It is a reminder to ingest the future official SSE notice, not a
    weekday-based trading calendar.
    """

    if not isinstance(payload, dict):
        raise ContractError("unverified trading-calendar candidate must be an object")
    if payload.get("schema_version") != CALENDAR_CANDIDATE_SCHEMA:
        raise ContractError("unsupported trading-calendar candidate schema")
    raw_year = payload.get("year")
    if isinstance(raw_year, bool):
        raise ContractError("trading-calendar candidate year is invalid")
    try:
        year = int(raw_year)
    except (TypeError, ValueError) as exc:
        raise ContractError("trading-calendar candidate year is invalid") from exc
    if expected_year is not None and year != expected_year:
        raise ContractError("trading-calendar candidate year does not match bundle")
    if payload.get("verification_status") != UNVERIFIED:
        raise ContractError("trading-calendar candidate must remain explicitly unverified")
    if payload.get("market") != "SSE" or payload.get("timezone") != "Asia/Shanghai":
        raise ContractError(
            "calendar candidate market/timezone must be SSE/Asia/Shanghai"
        )
    if payload.get("closed_dates") is not None or "open_dates" in payload:
        raise ContractError(
            "unverified calendar must not contain guessed open or closed dates"
        )
    if str(payload.get("source_discovery_url") or "").strip() != OFFICIAL_SSE_CALENDAR_INDEX:
        raise ContractError("calendar candidate must point to the official SSE index")
    checked_at = str(payload.get("checked_at") or "").strip()
    try:
        checked_date = datetime.strptime(checked_at, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ContractError("calendar candidate checked_at must be YYYY-MM-DD") from exc
    if checked_date.year > year:
        raise ContractError("calendar candidate checked_at cannot be after its year")
    return {
        "schema_version": CALENDAR_CANDIDATE_SCHEMA,
        "market": str(payload.get("market") or "SSE"),
        "year": year,
        "timezone": str(payload.get("timezone") or "Asia/Shanghai"),
        "verification_status": UNVERIFIED,
        "checked_at": checked_at,
        "source_discovery_url": OFFICIAL_SSE_CALENDAR_INDEX,
        "closed_dates": None,
        "notes": str(payload.get("notes") or "").strip(),
    }


def generate_unverified_calendar_candidate(
    year: int,
    *,
    checked_at: str,
) -> dict[str, Any]:
    if isinstance(year, bool) or not isinstance(year, int) or year < 2000:
        raise ContractError("calendar candidate year must be an integer")
    payload = {
        "schema_version": CALENDAR_CANDIDATE_SCHEMA,
        "market": "SSE",
        "year": year,
        "timezone": "Asia/Shanghai",
        "verification_status": UNVERIFIED,
        "checked_at": checked_at,
        "source_discovery_url": OFFICIAL_SSE_CALENDAR_INDEX,
        "closed_dates": None,
        "notes": (
            "No official SSE annual closure notice was available at checked_at. "
            "This metadata-only candidate is forbidden in production until an "
            "official notice is ingested and the bundle entry becomes VERIFIED."
        ),
    }
    return validate_unverified_calendar_candidate(payload, expected_year=year)


class TradingCalendarBundle:
    """Multi-year SSE calendar registry with fail-closed year promotion."""

    def __init__(
        self,
        entries: dict[int, CalendarBundleEntry],
        calendars: dict[int, TradingCalendar],
        candidates: dict[int, dict[str, Any]],
        *,
        market: str,
        timezone: str,
    ) -> None:
        self.entries = MappingProxyType(dict(entries))
        self.calendars = MappingProxyType(dict(calendars))
        self.candidates = MappingProxyType(
            {year: MappingProxyType(dict(payload)) for year, payload in candidates.items()}
        )
        self.market = market
        self.timezone = timezone

    @classmethod
    def from_file(
        cls,
        path: str | Path = DEFAULT_CALENDAR_PATH,
    ) -> "TradingCalendarBundle":
        bundle_path = Path(path)
        payload = _load_json(bundle_path, "trading calendar bundle")
        if payload.get("schema_version") != CALENDAR_BUNDLE_SCHEMA:
            raise ContractError("unsupported trading calendar bundle schema")
        if payload.get("market") != "SSE" or payload.get("timezone") != "Asia/Shanghai":
            raise ContractError(
                "calendar bundle market/timezone must be SSE/Asia/Shanghai"
            )
        raw_years = payload.get("years")
        if not isinstance(raw_years, list) or not raw_years:
            raise ContractError("trading calendar bundle years must be a non-empty list")
        entries: dict[int, CalendarBundleEntry] = {}
        calendars: dict[int, TradingCalendar] = {}
        candidates: dict[int, dict[str, Any]] = {}
        for index, raw in enumerate(raw_years):
            if not isinstance(raw, dict):
                raise ContractError(f"calendar bundle years[{index}] must be an object")
            raw_year = raw.get("year")
            if isinstance(raw_year, bool):
                raise ContractError("calendar bundle year must be an integer")
            try:
                year = int(raw_year)
            except (TypeError, ValueError) as exc:
                raise ContractError("calendar bundle year must be an integer") from exc
            if year in entries:
                raise ContractError(f"duplicate calendar bundle year: {year}")
            status = str(raw.get("status") or "").strip()
            if status not in {VERIFIED, UNVERIFIED}:
                raise ContractError(f"unsupported calendar status for {year}: {status!r}")
            relative_path = str(raw.get("path") or "").strip()
            entry_path = _safe_relative_path(
                bundle_path.parent,
                relative_path,
                f"calendar bundle years[{index}].path",
            )
            entry = CalendarBundleEntry(year=year, status=status, path=relative_path)
            entries[year] = entry
            if status == VERIFIED:
                calendar = TradingCalendar.from_file(entry_path)
                if calendar.year != year:
                    raise ContractError("verified calendar year does not match bundle entry")
                calendars[year] = calendar
            else:
                candidate = validate_unverified_calendar_candidate(
                    _load_json(entry_path, "trading calendar candidate"),
                    expected_year=year,
                )
                candidates[year] = candidate
        return cls(
            entries,
            calendars,
            candidates,
            market=str(payload.get("market") or "SSE"),
            timezone=str(payload.get("timezone") or "Asia/Shanghai"),
        )

    def _calendar_for(self, value: date, field_name: str) -> TradingCalendar:
        entry = self.entries.get(value.year)
        if entry is None:
            raise ContractError(
                f"{field_name}={value:%Y%m%d} has no calendar bundle entry"
            )
        if entry.status != VERIFIED:
            raise ContractError(
                f"{field_name}={value:%Y%m%d} calendar year {value.year} is not VERIFIED; "
                "production must fail closed until the official SSE notice is ingested"
            )
        calendar = self.calendars.get(value.year)
        if calendar is None:
            raise ContractError(f"verified calendar year {value.year} is not loaded")
        return calendar

    def is_open(self, value: date, field_name: str = "date") -> bool:
        return self._calendar_for(value, field_name).is_open(value, field_name)

    def next_open_date(self, value: date, field_name: str = "date") -> date:
        # The current day must itself be backed by a verified annual calendar;
        # crossing a year boundary is allowed only when the next annual file is
        # separately VERIFIED in the bundle.
        self._calendar_for(value, field_name)
        cursor = value
        for _ in range(40):
            cursor += timedelta(days=1)
            calendar = self._calendar_for(cursor, field_name)
            if calendar.is_open(cursor, field_name):
                return cursor
        raise ContractError(f"no next SSE trading day found after {value:%Y%m%d}")

    def validate_d_t_t1(self, decision_date: str, buy_date: str, exit_date: str) -> None:
        _validate_d_t_t1(self, decision_date, buy_date, exit_date)


TradingCalendarLike = TradingCalendar | TradingCalendarBundle


def load_trading_calendar(
    path: str | Path = DEFAULT_CALENDAR_PATH,
) -> TradingCalendarLike:
    calendar_path = Path(path)
    payload = _load_json(calendar_path, "trading calendar")
    schema = payload.get("schema_version")
    if schema == SINGLE_YEAR_CALENDAR_SCHEMA:
        return TradingCalendar.from_payload(payload, source_path=calendar_path)
    if schema == CALENDAR_BUNDLE_SCHEMA:
        return TradingCalendarBundle.from_file(calendar_path)
    raise ContractError(f"unsupported trading calendar schema: {schema!r}")


def _validate_d_t_t1(
    calendar: TradingCalendarLike,
    decision_date: str,
    buy_date: str,
    exit_date: str,
) -> None:
    d = parse_calendar_date(decision_date, "D")
    t = parse_calendar_date(buy_date, "T")
    t1 = parse_calendar_date(exit_date, "T+1")
    if not calendar.is_open(d, "D"):
        raise ContractError(f"D={decision_date} is not an SSE trading day")
    expected_t = calendar.next_open_date(d, "T")
    if t != expected_t:
        raise ContractError(
            f"T={buy_date} is not the next SSE trading day after D={decision_date}; "
            f"expected {expected_t:%Y%m%d}"
        )
    expected_t1 = calendar.next_open_date(t, "T+1")
    if t1 != expected_t1:
        raise ContractError(
            f"T+1={exit_date} is not the next SSE trading day after T={buy_date}; "
            f"expected {expected_t1:%Y%m%d}"
        )


__all__ = [
    "CALENDAR_BUNDLE_SCHEMA",
    "CALENDAR_CANDIDATE_SCHEMA",
    "DEFAULT_CALENDAR_PATH",
    "DEFAULT_SINGLE_YEAR_CALENDAR_PATH",
    "OFFICIAL_SSE_CALENDAR_INDEX",
    "SINGLE_YEAR_CALENDAR_SCHEMA",
    "TradingCalendar",
    "TradingCalendarBundle",
    "TradingCalendarLike",
    "UNVERIFIED",
    "VERIFIED",
    "generate_unverified_calendar_candidate",
    "load_trading_calendar",
    "parse_calendar_date",
    "validate_unverified_calendar_candidate",
]
