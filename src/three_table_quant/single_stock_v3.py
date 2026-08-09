from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

from .domain import ContractError, normalize_date, normalize_ts_code
from .security_master import ALLOWED_BOARDS
from .sources import SOURCE_IDS


SINGLE_STOCK_V3_SCHEMA = "single_stock_snapshot_v3"
STRICT_INTERSECTION_RULE = "STRICT_THREE_TABLE_INTERSECTION"

# These are facts, not model features.  The P0 gate deliberately contains only
# deterministic tradeability/data-contract checks; predictive filters belong
# in a later challenger and must not silently change intersection membership.
SUSPENDED_FIELD = "security.is_suspended"
ST_FIELD = "security.is_st"
DELISTING_FIELD = "security.is_delisting_period"
TRADING_RULES_VERIFIED_FIELD = "security.trading_rules_verified"
SECURITY_BOARD_FIELD = "security.board"
SECURITY_PRICE_LIMIT_PCT_FIELD = "security.price_limit_pct"
SECURITY_PRICE_TICK_FIELD = "security.price_tick"
PRICING_VERIFIED_FIELD = "market.pricing_verified"
D_CLOSE_FIELD = "market.d_close"
PRICE_TICK_FIELD = "market.price_tick"
BOARD_LOT_FIELD = "execution.board_lot"
MAX_ORDER_SHARES_FIELD = "execution.max_order_shares"

HARD_GATE_REQUIRED_FIELDS = (
    SUSPENDED_FIELD,
    ST_FIELD,
    DELISTING_FIELD,
    TRADING_RULES_VERIFIED_FIELD,
    SECURITY_BOARD_FIELD,
    SECURITY_PRICE_LIMIT_PCT_FIELD,
    SECURITY_PRICE_TICK_FIELD,
    PRICING_VERIFIED_FIELD,
    D_CLOSE_FIELD,
    PRICE_TICK_FIELD,
    BOARD_LOT_FIELD,
    MAX_ORDER_SHARES_FIELD,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class HardGateStatus(str, Enum):
    PASS = "PASS"
    BLOCK = "BLOCK"
    UNKNOWN = "UNKNOWN"


def _text(value: Any, field_name: str) -> str:
    parsed = str(value or "").strip()
    if not parsed:
        raise ContractError(f"{field_name} must be non-empty")
    return parsed


def _timestamp(value: Any, field_name: str) -> datetime:
    raw = _text(value, field_name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{field_name} must include a timezone offset")
    return parsed


def _freeze(value: Any, field_name: str = "value") -> Any:
    """Deep-freeze a JSON-like value while preserving ``None`` as missing."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError(f"{field_name} must not contain NaN or infinity")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            normalized_key = _text(key, f"{field_name} key")
            if normalized_key in frozen:
                raise ContractError(f"{field_name} contains duplicate normalized keys")
            frozen[normalized_key] = _freeze(item, f"{field_name}.{normalized_key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item, f"{field_name}[]") for item in value)
    raise ContractError(
        f"{field_name} must be a JSON-compatible scalar, mapping, list or tuple"
    )


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class FactProvenance:
    """Point-in-time lineage for one single-stock fact.

    ``known_at`` is the earliest time at which the system could have observed
    the value.  It, rather than ``event_at`` or fetch time, controls D-as-of
    eligibility.  ``event_at`` may legitimately be later for a pre-announced
    corporate event.
    """

    provider: str
    dataset_version: str
    known_at: str
    fetched_at: str
    content_sha256: str
    revision_id: str | None = None
    event_at: str | None = None
    source_uri: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _text(self.provider, "provider"))
        object.__setattr__(
            self,
            "dataset_version",
            _text(self.dataset_version, "dataset_version"),
        )
        known = _timestamp(self.known_at, "known_at")
        fetched = _timestamp(self.fetched_at, "fetched_at")
        if fetched.astimezone(timezone.utc) < known.astimezone(timezone.utc):
            raise ContractError("fetched_at cannot be earlier than known_at")
        if self.event_at is not None:
            _timestamp(self.event_at, "event_at")
        digest = str(self.content_sha256 or "").strip().lower()
        if _SHA256_RE.fullmatch(digest) is None:
            raise ContractError("content_sha256 must be 64 lowercase hex characters")
        object.__setattr__(self, "content_sha256", digest)
        if self.revision_id is not None:
            object.__setattr__(
                self,
                "revision_id",
                _text(self.revision_id, "revision_id"),
            )
        if self.source_uri is not None:
            object.__setattr__(self, "source_uri", _text(self.source_uri, "source_uri"))

    def validate_asof(self, decision_asof: datetime, field_name: str) -> None:
        known = _timestamp(self.known_at, f"{field_name}.known_at")
        if known.astimezone(timezone.utc) > decision_asof.astimezone(timezone.utc):
            raise ContractError(
                f"{field_name}.known_at is later than decision_asof; "
                "future information is forbidden"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "dataset_version": self.dataset_version,
            "known_at": self.known_at,
            "fetched_at": self.fetched_at,
            "content_sha256": self.content_sha256,
            "revision_id": self.revision_id,
            "event_at": self.event_at,
            "source_uri": self.source_uri,
        }


@dataclass(frozen=True)
class SingleStockFact:
    value: Any
    provenance: FactProvenance
    missing_reason: str | None = None
    unit: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.provenance, FactProvenance):
            raise ContractError("fact provenance must be FactProvenance")
        frozen_value = _freeze(self.value)
        object.__setattr__(self, "value", frozen_value)
        if frozen_value is None:
            object.__setattr__(
                self,
                "missing_reason",
                _text(self.missing_reason, "missing_reason"),
            )
        elif self.missing_reason is not None:
            raise ContractError("missing_reason is only valid when fact value is None")
        if self.unit is not None:
            object.__setattr__(self, "unit", _text(self.unit, "unit"))

    @classmethod
    def missing(
        cls,
        reason: str,
        provenance: FactProvenance,
        *,
        unit: str | None = None,
    ) -> SingleStockFact:
        return cls(None, provenance, missing_reason=reason, unit=unit)

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": _thaw(self.value),
            "missing_reason": self.missing_reason,
            "unit": self.unit,
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True)
class HardGateDecision:
    status: HardGateStatus
    reasons: tuple[str, ...]
    unknown_fields: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reasons": list(self.reasons),
            "unknown_fields": list(self.unknown_fields),
        }


def _fact_value(facts: Mapping[str, SingleStockFact], field_name: str) -> Any:
    fact = facts.get(field_name)
    return None if fact is None else fact.value


def _require_bool(value: Any, field_name: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ContractError(f"{field_name} must be boolean or explicitly missing")
    return value


def _positive_number(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ContractError(f"{field_name} must be a positive finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{field_name} must be a positive finite number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ContractError(f"{field_name} must be a positive finite number")
    return parsed


def _integer(value: Any, field_name: str, *, positive: bool) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{field_name} must be an integer")
    if (positive and value <= 0) or (not positive and value < 0):
        qualifier = "positive" if positive else "nonnegative"
        raise ContractError(f"{field_name} must be {qualifier}")
    return value


def _security_board(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContractError(f"{SECURITY_BOARD_FIELD} must be a string or explicitly missing")
    board = value.strip().upper()
    if board not in ALLOWED_BOARDS:
        raise ContractError(f"unsupported {SECURITY_BOARD_FIELD}: {value!r}")
    return board


def evaluate_hard_gate(
    facts: Mapping[str, SingleStockFact],
) -> HardGateDecision:
    """Evaluate deterministic tradeability without removing the candidate.

    Precedence is ``BLOCK`` over ``UNKNOWN`` over ``PASS``.  Therefore known
    suspension evidence remains a block even if another provider field is
    missing.  The caller still retains the candidate and serializes the gate.
    """

    suspended = _require_bool(_fact_value(facts, SUSPENDED_FIELD), SUSPENDED_FIELD)
    is_st = _require_bool(_fact_value(facts, ST_FIELD), ST_FIELD)
    delisting = _require_bool(_fact_value(facts, DELISTING_FIELD), DELISTING_FIELD)
    rules_verified = _require_bool(
        _fact_value(facts, TRADING_RULES_VERIFIED_FIELD),
        TRADING_RULES_VERIFIED_FIELD,
    )
    pricing_verified = _require_bool(
        _fact_value(facts, PRICING_VERIFIED_FIELD),
        PRICING_VERIFIED_FIELD,
    )
    _positive_number(_fact_value(facts, D_CLOSE_FIELD), D_CLOSE_FIELD)
    _security_board(_fact_value(facts, SECURITY_BOARD_FIELD))
    _positive_number(
        _fact_value(facts, SECURITY_PRICE_LIMIT_PCT_FIELD),
        SECURITY_PRICE_LIMIT_PCT_FIELD,
    )
    _positive_number(
        _fact_value(facts, SECURITY_PRICE_TICK_FIELD),
        SECURITY_PRICE_TICK_FIELD,
    )
    _positive_number(_fact_value(facts, PRICE_TICK_FIELD), PRICE_TICK_FIELD)
    board_lot = _integer(
        _fact_value(facts, BOARD_LOT_FIELD),
        BOARD_LOT_FIELD,
        positive=True,
    )
    max_order_shares = _integer(
        _fact_value(facts, MAX_ORDER_SHARES_FIELD),
        MAX_ORDER_SHARES_FIELD,
        positive=False,
    )

    blockers: list[str] = []
    if suspended is True:
        blockers.append("SUSPENDED")
    if is_st is True:
        blockers.append("SPECIAL_TREATMENT")
    if delisting is True:
        blockers.append("DELISTING_PERIOD")
    if rules_verified is False:
        blockers.append("TRADING_RULES_UNVERIFIED")
    if pricing_verified is False:
        blockers.append("PRICING_UNVERIFIED")
    if (
        board_lot is not None
        and max_order_shares is not None
        and max_order_shares < board_lot
    ):
        blockers.append("CAPACITY_BELOW_ONE_BOARD_LOT")

    unknown = tuple(
        field_name
        for field_name in HARD_GATE_REQUIRED_FIELDS
        if field_name not in facts or facts[field_name].value is None
    )
    if blockers:
        return HardGateDecision(HardGateStatus.BLOCK, tuple(blockers), unknown)
    if unknown:
        return HardGateDecision(
            HardGateStatus.UNKNOWN,
            ("REQUIRED_GATE_EVIDENCE_MISSING",),
            unknown,
        )
    return HardGateDecision(HardGateStatus.PASS, (), ())


@dataclass(frozen=True)
class SingleStockSnapshotV3:
    """Immutable, D-as-of record for one strict-intersection candidate."""

    ts_code: str
    name: str
    decision_date: str
    decision_asof: str
    source_ranks: Mapping[str, int]
    facts: Mapping[str, SingleStockFact]
    schema_version: str = field(init=False, default=SINGLE_STOCK_V3_SCHEMA)
    selection_rule: str = field(init=False, default=STRICT_INTERSECTION_RULE)
    hard_gate: HardGateDecision = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts_code", normalize_ts_code(self.ts_code))
        object.__setattr__(self, "name", _text(self.name, "name"))
        object.__setattr__(
            self,
            "decision_date",
            normalize_date(self.decision_date, "decision_date"),
        )
        decision_asof = _timestamp(self.decision_asof, "decision_asof")
        if (
            decision_asof.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
            != self.decision_date
        ):
            raise ContractError(
                "decision_asof must fall on decision_date in Asia/Shanghai; "
                "late replay cannot masquerade as a D-as-of snapshot"
            )

        if not isinstance(self.source_ranks, Mapping):
            raise ContractError("source_ranks must be a mapping")
        if set(self.source_ranks) != set(SOURCE_IDS):
            raise ContractError(
                "single-stock V3 snapshots require exact membership in all three sources"
            )
        ranks: dict[str, int] = {}
        for source_id in SOURCE_IDS:
            rank = self.source_ranks[source_id]
            if isinstance(rank, bool) or not isinstance(rank, int) or rank <= 0:
                raise ContractError(f"source_ranks.{source_id} must be a positive integer")
            ranks[source_id] = rank
        object.__setattr__(self, "source_ranks", MappingProxyType(ranks))

        if not isinstance(self.facts, Mapping):
            raise ContractError("facts must be a mapping")
        normalized_facts: dict[str, SingleStockFact] = {}
        for raw_name, fact in sorted(self.facts.items(), key=lambda pair: str(pair[0])):
            field_name = _text(raw_name, "fact field name")
            if field_name in normalized_facts:
                raise ContractError("facts contain duplicate normalized field names")
            if not isinstance(fact, SingleStockFact):
                raise ContractError(f"{field_name} must be SingleStockFact")
            fact.provenance.validate_asof(decision_asof, field_name)
            normalized_facts[field_name] = fact
        frozen_facts = MappingProxyType(normalized_facts)
        object.__setattr__(self, "facts", frozen_facts)
        object.__setattr__(self, "hard_gate", evaluate_hard_gate(frozen_facts))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "selection_rule": self.selection_rule,
            "ts_code": self.ts_code,
            "name": self.name,
            "decision_date": self.decision_date,
            "decision_asof": self.decision_asof,
            "source_ranks": dict(self.source_ranks),
            "facts": {
                field_name: fact.to_dict()
                for field_name, fact in self.facts.items()
            },
            "hard_gate": self.hard_gate.to_dict(),
        }
