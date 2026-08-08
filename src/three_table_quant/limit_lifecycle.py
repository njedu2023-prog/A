from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .domain import normalize_date


LIMIT_LIFECYCLE_SCHEMA_VERSION = "limit_lifecycle_v1"
MINUTE_CLOSE_PROXY = "MINUTE_CLOSE_PROXY"


def _session_minutes() -> tuple[str, ...]:
    minutes: list[str] = []
    for start_text, count in (("09:30", 120), ("13:00", 120)):
        start = datetime.strptime(start_text, "%H:%M")
        minutes.extend(
            (start + timedelta(minutes=offset)).strftime("%H:%M")
            for offset in range(count)
        )
    return tuple(minutes)


EXPECTED_SESSION_MINUTES = _session_minutes()
EXPECTED_SESSION_MINUTE_SET = frozenset(EXPECTED_SESSION_MINUTES)
TAIL_SESSION_START = "14:30"

SERIALIZED_LIMIT_LIFECYCLE_FIELDS = frozenset(
    {
        "schema_version",
        "decision_date",
        "evidence_level",
        "valid",
        "invalid_reasons",
        "limit_price",
        "price_tick",
        "expected_minutes",
        "observed_minutes",
        "coverage_ratio",
        "touched_limit",
        "closed_at_limit",
        "first_seal_time",
        "last_seal_time",
        "break_count",
        "reseal_count",
        "sealed_minutes",
        "tail_sealed_minutes",
        "one_price_limit_proxy",
    }
)


@dataclass(frozen=True, slots=True)
class LimitLifecycleSnapshot:
    """Immutable D-day limit-up lifecycle derived from minute-close evidence.

    Minute bars cannot prove order-book sealing.  A minute whose close equals
    the frozen limit price is therefore only a *seal proxy*.  Any contract or
    coverage failure nulls every derived lifecycle field so that missing
    evidence is never represented as a real zero.
    """

    schema_version: str
    decision_date: str | None
    evidence_level: str
    valid: bool
    invalid_reasons: tuple[str, ...]
    limit_price: float | None
    price_tick: float | None
    expected_minutes: int
    observed_minutes: int | None
    coverage_ratio: float | None
    touched_limit: bool | None
    closed_at_limit: bool | None
    first_seal_time: str | None
    last_seal_time: str | None
    break_count: int | None
    reseal_count: int | None
    sealed_minutes: int | None
    tail_sealed_minutes: int | None
    one_price_limit_proxy: bool | None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["invalid_reasons"] = list(self.invalid_reasons)
        return payload


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _positive_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _nonnegative_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _on_tick(value: Any, tick: Any) -> bool:
    parsed_value = _decimal(value)
    parsed_tick = _decimal(tick)
    if (
        parsed_value is None
        or parsed_tick is None
        or parsed_value <= 0
        or parsed_tick <= 0
    ):
        return False
    units = parsed_value / parsed_tick
    return units == units.to_integral_value()


def _same_price(left: float, right: float, tick: float) -> bool:
    return math.isclose(left, right, rel_tol=0.0, abs_tol=tick * 1e-6)


def _normalized_time(value: Any) -> str | None:
    text = str(value or "").strip()
    if len(text) != 5:
        return None
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError:
        return None
    normalized = parsed.strftime("%H:%M")
    return normalized if normalized == text else None


def _invalid_snapshot(
    *,
    decision_date: str | None,
    reasons: list[str],
    limit_price: float | None,
    price_tick: float | None,
    observed_minutes: int | None,
    coverage_ratio: float | None,
) -> LimitLifecycleSnapshot:
    return LimitLifecycleSnapshot(
        schema_version=LIMIT_LIFECYCLE_SCHEMA_VERSION,
        decision_date=decision_date,
        evidence_level=MINUTE_CLOSE_PROXY,
        valid=False,
        invalid_reasons=tuple(dict.fromkeys(reasons)),
        limit_price=limit_price,
        price_tick=price_tick,
        expected_minutes=len(EXPECTED_SESSION_MINUTES),
        observed_minutes=observed_minutes,
        coverage_ratio=coverage_ratio,
        touched_limit=None,
        closed_at_limit=None,
        first_seal_time=None,
        last_seal_time=None,
        break_count=None,
        reseal_count=None,
        sealed_minutes=None,
        tail_sealed_minutes=None,
        one_price_limit_proxy=None,
    )


def validate_serialized_limit_lifecycle(
    payload: Any,
    *,
    expected_decision_date: str,
    expected_limit_price: float | None = None,
    expected_price_tick: float | None = None,
) -> None:
    """Validate the exact serialized minute-close lifecycle contract.

    This validates internal consistency only.  Passing it never upgrades
    ``MINUTE_CLOSE_PROXY`` into proof of an order-book seal, a real board break,
    a reseal, queue priority or executable liquidity.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("serialized limit lifecycle must be an object")
    if set(payload) != SERIALIZED_LIMIT_LIFECYCLE_FIELDS:
        raise ValueError("serialized limit lifecycle fields do not match schema")
    if payload.get("schema_version") != LIMIT_LIFECYCLE_SCHEMA_VERSION:
        raise ValueError("serialized limit lifecycle schema_version mismatch")
    if payload.get("evidence_level") != MINUTE_CLOSE_PROXY:
        raise ValueError("serialized limit lifecycle must remain MINUTE_CLOSE_PROXY")
    if payload.get("valid") is not True:
        raise ValueError("serialized limit lifecycle must be valid")
    if payload.get("invalid_reasons") != []:
        raise ValueError("valid serialized limit lifecycle requires empty invalid_reasons")

    expected_day = normalize_date(
        expected_decision_date,
        "serialized lifecycle expected_decision_date",
    )
    if payload.get("decision_date") != expected_day:
        raise ValueError("serialized limit lifecycle decision_date mismatch")

    expected_count = len(EXPECTED_SESSION_MINUTES)
    for field_name in ("expected_minutes", "observed_minutes"):
        value = payload.get(field_name)
        if type(value) is not int or value != expected_count:
            raise ValueError(
                f"serialized limit lifecycle {field_name} must equal {expected_count}"
            )
    coverage = payload.get("coverage_ratio")
    if (
        isinstance(coverage, bool)
        or not isinstance(coverage, (int, float))
        or not math.isfinite(float(coverage))
        or not math.isclose(float(coverage), 1.0, rel_tol=0.0, abs_tol=1e-12)
    ):
        raise ValueError("serialized limit lifecycle coverage_ratio must equal 1")

    limit_price = _positive_float(payload.get("limit_price"))
    if limit_price is None:
        raise ValueError("serialized limit lifecycle limit_price must be positive")
    price_tick = _positive_float(payload.get("price_tick"))
    if price_tick is None:
        raise ValueError("serialized limit lifecycle price_tick must be positive")
    if not _on_tick(limit_price, price_tick):
        raise ValueError("serialized limit lifecycle limit_price must be on tick")

    if expected_price_tick is not None:
        normalized_expected_tick = _positive_float(expected_price_tick)
        if normalized_expected_tick is None:
            raise ValueError("expected_price_tick must be positive")
        if not math.isclose(
            price_tick,
            normalized_expected_tick,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("serialized limit lifecycle price_tick mismatch")
    if expected_limit_price is not None:
        normalized_expected_limit = _positive_float(expected_limit_price)
        if normalized_expected_limit is None:
            raise ValueError("expected_limit_price must be positive")
        if not _on_tick(normalized_expected_limit, price_tick):
            raise ValueError("expected_limit_price must be on serialized price tick")
        if not _same_price(limit_price, normalized_expected_limit, price_tick):
            raise ValueError("serialized limit lifecycle limit_price mismatch")

    for field_name in (
        "touched_limit",
        "closed_at_limit",
        "one_price_limit_proxy",
    ):
        if type(payload.get(field_name)) is not bool:
            raise ValueError(
                f"serialized limit lifecycle {field_name} must be boolean"
            )

    counts: dict[str, int] = {}
    for field_name in (
        "break_count",
        "reseal_count",
        "sealed_minutes",
        "tail_sealed_minutes",
    ):
        value = payload.get(field_name)
        if type(value) is not int or not 0 <= value <= expected_count:
            raise ValueError(
                f"serialized limit lifecycle {field_name} must be an integer in 0..{expected_count}"
            )
        counts[field_name] = value
    if counts["tail_sealed_minutes"] > counts["sealed_minutes"]:
        raise ValueError("tail_sealed_minutes cannot exceed sealed_minutes")
    if counts["reseal_count"] > counts["break_count"]:
        raise ValueError("reseal_count cannot exceed break_count")

    first_time = payload.get("first_seal_time")
    last_time = payload.get("last_seal_time")
    for field_name, value in (
        ("first_seal_time", first_time),
        ("last_seal_time", last_time),
    ):
        if value is not None and (
            not isinstance(value, str) or value not in EXPECTED_SESSION_MINUTE_SET
        ):
            raise ValueError(
                f"serialized limit lifecycle {field_name} must be a session minute or null"
            )
    if (first_time is None) != (last_time is None):
        raise ValueError("first_seal_time and last_seal_time must both exist or both be null")
    if first_time is not None and last_time is not None:
        if EXPECTED_SESSION_MINUTES.index(first_time) > EXPECTED_SESSION_MINUTES.index(
            last_time
        ):
            raise ValueError("first_seal_time cannot be later than last_seal_time")

    sealed_minutes = counts["sealed_minutes"]
    if sealed_minutes == 0:
        if first_time is not None or last_time is not None:
            raise ValueError("zero sealed_minutes requires null seal times")
        if payload["closed_at_limit"] is not False:
            raise ValueError("zero sealed_minutes requires closed_at_limit=false")
        if counts["break_count"] != 0 or counts["reseal_count"] != 0:
            raise ValueError("zero sealed_minutes requires zero break and reseal counts")
    elif first_time is None or last_time is None:
        raise ValueError("positive sealed_minutes requires both seal times")

    if payload["one_price_limit_proxy"] is True:
        required = {
            "sealed_minutes": expected_count,
            "first_seal_time": EXPECTED_SESSION_MINUTES[0],
            "last_seal_time": EXPECTED_SESSION_MINUTES[-1],
            "closed_at_limit": True,
            "touched_limit": True,
            "break_count": 0,
            "reseal_count": 0,
        }
        for field_name, expected_value in required.items():
            if payload.get(field_name) != expected_value:
                raise ValueError(
                    "one_price_limit_proxy has inconsistent "
                    f"{field_name}"
                )


def build_limit_lifecycle(
    bars: Sequence[Any],
    decision_date: str,
    limit_price: float,
    price_tick: float = 0.01,
) -> LimitLifecycleSnapshot:
    """Build a fail-closed D-day limit-up lifecycle from one-minute bars.

    Valid evidence must contain every interval-start minute in the two regular
    A-share continuous-auction sessions: 09:30--11:29 and 13:00--14:59.  This
    deliberately excludes call-auction order-book claims, which minute OHLC
    bars cannot establish.
    """

    reasons: list[str] = []
    try:
        day = normalize_date(decision_date, "limit lifecycle decision_date")
    except Exception:
        day = None
        reasons.append("invalid_decision_date")

    normalized_limit = _positive_float(limit_price)
    if normalized_limit is None:
        reasons.append("invalid_limit_price")
    normalized_tick = _positive_float(price_tick)
    if normalized_tick is None:
        reasons.append("invalid_price_tick")
    if (
        normalized_limit is not None
        and normalized_tick is not None
        and not _on_tick(normalized_limit, normalized_tick)
    ):
        reasons.append("limit_price_off_tick")

    normalized: list[tuple[str, str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    observed_d_times: set[str] = set()

    for bar in bars:
        try:
            bar_day = normalize_date(
                getattr(bar, "date", None), "limit lifecycle bar date"
            )
        except Exception:
            reasons.append("invalid_bar_date")
            continue
        bar_time = _normalized_time(getattr(bar, "time", None))
        if bar_time is None:
            reasons.append("invalid_bar_time")
            continue

        key = (bar_day, bar_time)
        if key in seen_keys:
            reasons.append("duplicate_minute_bar")
            continue
        seen_keys.add(key)
        normalized.append((bar_day, bar_time, bar))

        if day is not None:
            if bar_day > day:
                reasons.append("future_bar_present")
            if bar_day != day:
                reasons.append("non_decision_date_bar_present")
            elif bar_time in EXPECTED_SESSION_MINUTE_SET:
                observed_d_times.add(bar_time)
            else:
                reasons.append("out_of_session_bar_present")

    if normalized != sorted(normalized, key=lambda item: (item[0], item[1])):
        reasons.append("bars_not_chronological")

    observed_minutes = len(observed_d_times) if day is not None else None
    coverage_ratio = (
        observed_minutes / len(EXPECTED_SESSION_MINUTES)
        if observed_minutes is not None
        else None
    )
    if day is not None and observed_d_times != EXPECTED_SESSION_MINUTE_SET:
        reasons.append("minute_coverage_incomplete")

    for bar_day, bar_time, bar in normalized:
        if str(getattr(bar, "price_adjustment", "")).upper() != "NONE":
            reasons.append("adjusted_minute_bar_present")
        if str(getattr(bar, "time_semantics", "")).upper() != "INTERVAL_START":
            reasons.append("unsupported_time_semantics")

        bar_tick = _positive_float(getattr(bar, "price_tick", None))
        if (
            normalized_tick is None
            or bar_tick is None
            or not math.isclose(
                bar_tick, normalized_tick, rel_tol=0.0, abs_tol=1e-12
            )
        ):
            reasons.append("inconsistent_price_tick")

        prices = [
            _positive_float(getattr(bar, field, None))
            for field in ("open", "close", "high", "low")
        ]
        volume = _nonnegative_float(getattr(bar, "volume", None))
        amount = _nonnegative_float(getattr(bar, "amount", None))
        if any(value is None for value in prices) or volume is None or amount is None:
            reasons.append("invalid_minute_ohlcv")
            continue
        open_price, close_price, high, low = (
            float(value) for value in prices if value is not None
        )
        if (
            high < low
            or low > min(open_price, close_price)
            or high < max(open_price, close_price)
        ):
            reasons.append("invalid_minute_ohlcv")
            continue
        if normalized_tick is not None and any(
            not _on_tick(value, normalized_tick)
            for value in (open_price, close_price, high, low)
        ):
            reasons.append("minute_price_off_tick")
        if (
            normalized_limit is not None
            and normalized_tick is not None
            and high > normalized_limit + normalized_tick * 1e-6
        ):
            reasons.append("price_above_limit")

    if reasons:
        return _invalid_snapshot(
            decision_date=day,
            reasons=reasons,
            limit_price=normalized_limit,
            price_tick=normalized_tick,
            observed_minutes=observed_minutes,
            coverage_ratio=coverage_ratio,
        )

    assert day is not None
    assert normalized_limit is not None
    assert normalized_tick is not None

    ordered_bars = [bar for _, _, bar in normalized]
    touched = any(
        _same_price(float(bar.high), normalized_limit, normalized_tick)
        for bar in ordered_bars
    )
    sealed = [
        _same_price(float(bar.close), normalized_limit, normalized_tick)
        for bar in ordered_bars
    ]
    sealed_times = [
        str(bar.time) for bar, is_sealed in zip(ordered_bars, sealed, strict=True)
        if is_sealed
    ]

    break_count = 0
    reseal_count = 0
    has_sealed = False
    has_broken = False
    previous = False
    for is_sealed in sealed:
        if is_sealed and not previous:
            if has_sealed and has_broken:
                reseal_count += 1
            has_sealed = True
            has_broken = False
        elif not is_sealed and previous:
            break_count += 1
            has_broken = True
        previous = is_sealed

    one_price_proxy = all(
        _same_price(float(getattr(bar, field)), normalized_limit, normalized_tick)
        for bar in ordered_bars
        for field in ("open", "close", "high", "low")
    )

    return LimitLifecycleSnapshot(
        schema_version=LIMIT_LIFECYCLE_SCHEMA_VERSION,
        decision_date=day,
        evidence_level=MINUTE_CLOSE_PROXY,
        valid=True,
        invalid_reasons=(),
        limit_price=normalized_limit,
        price_tick=normalized_tick,
        expected_minutes=len(EXPECTED_SESSION_MINUTES),
        observed_minutes=len(ordered_bars),
        coverage_ratio=1.0,
        touched_limit=touched,
        closed_at_limit=sealed[-1],
        first_seal_time=sealed_times[0] if sealed_times else None,
        last_seal_time=sealed_times[-1] if sealed_times else None,
        break_count=break_count,
        reseal_count=reseal_count,
        sealed_minutes=sum(sealed),
        tail_sealed_minutes=sum(
            is_sealed and str(bar.time) >= TAIL_SESSION_START
            for bar, is_sealed in zip(ordered_bars, sealed, strict=True)
        ),
        one_price_limit_proxy=one_price_proxy,
    )


__all__ = [
    "EXPECTED_SESSION_MINUTES",
    "LIMIT_LIFECYCLE_SCHEMA_VERSION",
    "MINUTE_CLOSE_PROXY",
    "SERIALIZED_LIMIT_LIFECYCLE_FIELDS",
    "LimitLifecycleSnapshot",
    "build_limit_lifecycle",
    "validate_serialized_limit_lifecycle",
]
