from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any


class ContractError(ValueError):
    """Raised when a source violates the frozen input contract."""


@dataclass(frozen=True)
class SourceRow:
    source_id: str
    rank: int
    ts_code: str
    name: str
    values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceTable:
    source_id: str
    decision_date: str
    buy_date: str
    exit_date: str
    rows: tuple[SourceRow, ...]
    url: str
    content_sha256: str
    generated_at: str | None = None
    remote_blob_sha: str | None = None

    def codes(self) -> set[str]:
        return {row.ts_code for row in self.rows}


@dataclass
class SourceIssue:
    code: str
    severity: str
    source_id: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Candidate:
    ts_code: str
    name: str
    source_ranks: dict[str, int]
    source_values: dict[str, dict[str, Any]]
    features: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    order_spec: dict[str, Any] = field(default_factory=dict)
    rank: int | None = None
    action: str = "NO_TRADE"
    action_reason: str = "not_scored"
    # Kept last to preserve every pre-V3 positional constructor argument.
    # It remains outside ``features`` and ``metrics`` so availability cannot
    # change strict-intersection membership, ranking, or the frozen order.
    single_stock_research: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Signal:
    signal_id: str
    decision_date: str
    buy_date: str
    exit_date: str
    generated_at: str
    source_snapshots: list[dict[str, Any]]
    candidates: list[Candidate]
    model_version: str
    status: str = "RANKED"
    market_data_provenance: dict[str, Any] = field(default_factory=dict)
    ranking_engine: dict[str, Any] = field(default_factory=dict)
    single_stock_research_schema_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["candidates"] = [candidate.to_dict() for candidate in self.candidates]
        return data


def normalize_date(value: Any, field_name: str) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) != 8:
        raise ContractError(f"{field_name} must be YYYYMMDD, got {value!r}")
    return digits


def iso_date(value: str) -> str:
    value = normalize_date(value, "date")
    return f"{value[:4]}-{value[4:6]}-{value[6:]}"


def normalize_ts_code(value: Any) -> str:
    raw = str(value or "").strip().upper().replace(" ", "")
    if not raw:
        raise ContractError("empty ts_code")
    if "." in raw:
        number, market = raw.split(".", 1)
    elif raw[:2] in {"SH", "SZ", "BJ"} and raw[2:].isdigit():
        market, number = raw[:2], raw[2:]
    else:
        number = "".join(ch for ch in raw if ch.isdigit())
        if number.startswith(("4", "8")):
            market = "BJ"
        elif number.startswith(("5", "6", "9")):
            market = "SH"
        else:
            market = "SZ"
    if len(number) != 6 or not number.isdigit() or market not in {"SH", "SZ", "BJ"}:
        raise ContractError(f"invalid ts_code: {value!r}")
    return f"{number}.{market}"


AUCTION_TRUTH_SCHEMA = "auction_execution_v2"


def _strict_nonnegative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ContractError(f"{field_name} must be an integer share quantity")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{field_name} must be an integer share quantity") from exc
    if str(value).strip() not in {str(parsed), f"{parsed}.0"} and not isinstance(value, int):
        raise ContractError(f"{field_name} must be an integer share quantity")
    if parsed < 0:
        raise ContractError(f"{field_name} must be nonnegative")
    return parsed


def _strict_positive_float(value: Any, field_name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{field_name} must be a positive finite number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ContractError(f"{field_name} must be a positive finite number")
    return parsed


def _is_price_tick(value: float, tick: float) -> bool:
    units = value / tick
    return math.isclose(units, round(units), rel_tol=0.0, abs_tol=1e-7)


@dataclass(frozen=True)
class AuctionTruth:
    """Validated T-day opening-call-auction execution truth.

    Version 2 is intentionally fail-closed. Legacy records remain readable JSON,
    but cannot create a fill until they are upgraded with the exact event,
    phase, source, quantity and participation evidence below.
    """

    event_at: str
    trade_date: str
    ts_code: str
    phase: str
    source: str
    data_tier: str
    label_quality: str
    submitted_qty: int
    filled_qty: int
    limit_price: float
    price: float | None
    auction_matched_qty: int | None
    queue_ahead_qty: int | None
    executable_qty_at_order: int | None
    participation_rate: float | None
    participation_cap_breached: bool
    reason: str | None
    price_limit_source: str

    @classmethod
    def from_record(
        cls,
        record: dict[str, Any],
        *,
        expected_date: str,
        expected_ts_code: str,
        execution: dict[str, Any],
    ) -> "AuctionTruth":
        schema = str(record.get("schema_version") or "")
        expected_schema = str(execution.get("auction_truth_schema", AUCTION_TRUTH_SCHEMA))
        if schema != expected_schema:
            raise ContractError(f"auction truth schema must be {expected_schema}")

        trade_date = normalize_date(record.get("trade_date"), "auction trade_date")
        if trade_date != normalize_date(expected_date, "expected auction date"):
            raise ContractError("auction truth trade_date does not match buy_date")
        ts_code = normalize_ts_code(record.get("ts_code"))
        if ts_code != normalize_ts_code(expected_ts_code):
            raise ContractError("auction truth ts_code does not match trade")

        event_at = str(record.get("event_at") or "")
        try:
            event_time = datetime.fromisoformat(event_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ContractError("auction event_at must be an ISO-8601 timestamp") from exc
        if event_time.utcoffset() != timedelta(hours=8):
            raise ContractError("auction event_at must use Asia/Shanghai +08:00")
        if event_time.strftime("%Y%m%d") != trade_date:
            raise ContractError("auction event_at date does not match trade_date")
        expected_time = str(execution.get("auction_time", "09:25"))
        if event_time.strftime("%H:%M") != expected_time or event_time.second != 0 or event_time.microsecond != 0:
            raise ContractError(f"auction event_at must be exactly {expected_time}:00")

        phase = str(record.get("phase") or "").upper()
        expected_phase = str(execution.get("auction_phase", "OPENING_CALL_AUCTION")).upper()
        if phase != expected_phase:
            raise ContractError(f"auction phase must be {expected_phase}")

        quality = str(record.get("label_quality") or "").upper()
        source = str(record.get("source") or "").upper()
        data_tier = str(record.get("data_tier") or "").upper()
        if quality not in {"ACTUAL", "REPLAY", "CONSERVATIVE"}:
            raise ContractError("auction label_quality is not executable evidence")
        if quality == "ACTUAL" and (source, data_tier) != ("BROKER_EXECUTION", "BROKER_LOG"):
            raise ContractError(
                "ACTUAL truth requires source=BROKER_EXECUTION and data_tier=BROKER_LOG"
            )
        if quality == "REPLAY" and (source, data_tier) != ("EXCHANGE_ORDER_REPLAY", "ORDERBOOK"):
            raise ContractError(
                "REPLAY truth requires source=EXCHANGE_ORDER_REPLAY and data_tier=ORDERBOOK"
            )
        if quality == "CONSERVATIVE" and (
            source != "CONSERVATIVE_QUEUE_MODEL" or data_tier not in {"ORDERBOOK", "AUCTION_AGGREGATE"}
        ):
            raise ContractError(
                "CONSERVATIVE truth requires source=CONSERVATIVE_QUEUE_MODEL and queue-capable evidence"
            )

        quantity_unit = str(record.get("quantity_unit") or "").upper()
        if quantity_unit != "SHARES":
            raise ContractError("auction quantities must declare quantity_unit=SHARES")
        submitted_qty = _strict_nonnegative_int(record.get("submitted_qty"), "submitted_qty")
        filled_qty = _strict_nonnegative_int(record.get("filled_qty"), "filled_qty")
        lot_size = int(execution.get("lot_size", 100))
        if submitted_qty <= 0 or submitted_qty % lot_size != 0:
            raise ContractError(f"submitted_qty must be a positive multiple of {lot_size}")
        if filled_qty > submitted_qty:
            raise ContractError("filled_qty cannot exceed submitted_qty")

        tick = float(execution.get("price_tick", 0.01))
        limit_price = _strict_positive_float(record.get("limit_price"), "limit_price")
        if not _is_price_tick(limit_price, tick):
            raise ContractError("limit_price is not aligned to the configured price tick")
        price_limit_source = str(record.get("price_limit_source") or "").upper()
        allowed_limit_sources = {
            str(item).upper()
            for item in execution.get(
                "accepted_price_limit_sources",
                ["EXCHANGE_SECURITY_MASTER", "BROKER_ORDER_RECORD"],
            )
        }
        if price_limit_source not in allowed_limit_sources:
            raise ContractError("price_limit_source is not accepted execution evidence")

        reserved_amount = submitted_qty * limit_price
        commission = max(
            float(execution.get("minimum_commission_cny", 5.0)),
            reserved_amount * float(execution.get("commission_rate", 0.0)),
        )
        reserved_total = reserved_amount + commission + reserved_amount * float(
            execution.get("transfer_fee_rate_each_side", 0.0)
        )
        if reserved_total > float(execution["slot_capital_cny"]) + 1e-6:
            raise ContractError("submitted order exceeds slot capital including buy fees")

        reason = str(record.get("reason") or "").upper() or None
        raw_price = record.get("price")
        raw_matched = record.get("auction_matched_qty")
        raw_queue_ahead = record.get("queue_ahead_qty")
        raw_executable = record.get("executable_qty_at_order")
        queue_ahead_qty: int | None = None
        executable_qty_at_order: int | None = None
        participation_rate: float | None = None
        participation_cap_breached = False
        if filled_qty == 0:
            allowed_zero_reasons = {
                str(item).upper()
                for item in execution.get(
                    "reliable_zero_fill_reasons",
                    [
                        "NO_AUCTION_MATCH",
                        "SUSPENDED",
                        "ORDER_REJECTED",
                        "QUEUE_NOT_REACHED",
                        "INSUFFICIENT_SELL_VOLUME",
                    ],
                )
            }
            if reason not in allowed_zero_reasons:
                raise ContractError("zero fill requires a reliable zero-fill reason")
            price = None if raw_price is None or raw_price == "" else _strict_positive_float(raw_price, "price")
            if price is not None and not _is_price_tick(price, tick):
                raise ContractError("auction price is not aligned to the configured price tick")
            auction_matched_qty = (
                None
                if raw_matched is None or raw_matched == ""
                else _strict_nonnegative_int(raw_matched, "auction_matched_qty")
            )
            if raw_queue_ahead is not None and raw_queue_ahead != "":
                queue_ahead_qty = _strict_nonnegative_int(raw_queue_ahead, "queue_ahead_qty")
            if raw_executable is not None and raw_executable != "":
                executable_qty_at_order = _strict_nonnegative_int(
                    raw_executable, "executable_qty_at_order"
                )
        else:
            price = _strict_positive_float(raw_price, "price")
            if not _is_price_tick(price, tick):
                raise ContractError("auction price is not aligned to the configured price tick")
            if price > limit_price + tick / 2.0:
                raise ContractError("auction fill price exceeds the submitted limit price")
            auction_matched_qty = _strict_nonnegative_int(raw_matched, "auction_matched_qty")
            if auction_matched_qty <= 0 or filled_qty > auction_matched_qty:
                raise ContractError("auction_matched_qty cannot support filled_qty")
            cap = float(execution.get("auction_participation_cap", 0.01))
            if not 0 < cap <= 1:
                raise ContractError("auction_participation_cap must be in (0, 1]")
            participation_rate = submitted_qty / auction_matched_qty
            participation_cap_breached = participation_rate > cap + 1e-12
            if quality != "ACTUAL" and participation_cap_breached:
                raise ContractError("submitted_qty exceeds auction participation cap")
            if quality != "ACTUAL":
                if data_tier != "ORDERBOOK":
                    raise ContractError("non-ACTUAL positive fill requires full order-book queue evidence")
                queue_ahead_qty = _strict_nonnegative_int(raw_queue_ahead, "queue_ahead_qty")
                executable_qty_at_order = _strict_nonnegative_int(
                    raw_executable, "executable_qty_at_order"
                )
                fillable_after_queue = max(0, executable_qty_at_order - queue_ahead_qty)
                if filled_qty > fillable_after_queue:
                    raise ContractError("queue evidence cannot support the claimed positive fill")

        return cls(
            event_at=event_at,
            trade_date=trade_date,
            ts_code=ts_code,
            phase=phase,
            source=source,
            data_tier=data_tier,
            label_quality=quality,
            submitted_qty=submitted_qty,
            filled_qty=filled_qty,
            limit_price=limit_price,
            price=price,
            auction_matched_qty=auction_matched_qty,
            queue_ahead_qty=queue_ahead_qty,
            executable_qty_at_order=executable_qty_at_order,
            participation_rate=participation_rate,
            participation_cap_breached=participation_cap_breached,
            reason=reason,
            price_limit_source=price_limit_source,
        )
