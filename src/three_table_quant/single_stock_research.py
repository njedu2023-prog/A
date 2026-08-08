from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

from .domain import Candidate, ContractError, normalize_date, normalize_ts_code
from .execution_policy import ORDER_SPEC_SCHEMA
from .limit_lifecycle import LimitLifecycleSnapshot
from .single_stock_v3 import SingleStockSnapshotV3


SINGLE_STOCK_RESEARCH_SCHEMA = "single_stock_research_audit_v1"
AUDIT_ONLY_EFFECT = "AUDIT_ONLY_NO_RANKING_OR_GATE_EFFECT"
RESEARCH_SNAPSHOT_HASH_FIELD = "snapshot_sha256"

# Only already-frozen D-day outputs are copied.  Raw source rows are excluded:
# they can contain realised columns beside predictors and are already represented
# by provenance-controlled facts in ``SingleStockSnapshotV3``.
CANDIDATE_FEATURE_AUDIT_FIELDS = (
    "feature_schema_version",
    "feature_asof_date",
    "feature_coverage",
    "market_data_valid",
    "market_data_invalid_reasons",
    "stage_transition",
    "ret_1d",
    "ret_5d",
    "ret_20d",
    "volatility_20d",
    "downside_volatility_20d",
    "cvar_loss_10pct",
    "max_drawdown_20d",
    "avg_amount_20d",
    "avg_volume_20d",
    "rank_consensus",
    "rank_disagreement",
)

CANDIDATE_METRIC_AUDIT_FIELDS = (
    "model_id",
    "model_stage",
    "prediction_schema_version",
    "expected_net_return",
    "conditional_net_return_mean",
    "conditional_net_return_q10",
    "conditional_net_return_q50",
    "conditional_net_return_q90",
    "expected_shortfall_10pct",
    "p_exit_delay",
    "expected_delay_days",
    "p_promotion",
    "uncertainty",
    "utility_score",
    "gate_decision",
    "gate_reasons",
    "ranking_fallback",
)

ORDER_SPEC_AUDIT_FIELDS = (
    "schema_version",
    "decision_date",
    "trade_date",
    "event_time",
    "phase",
    "side",
    "order_type",
    "limit_price_policy",
    "limit_price",
    "price_limit_source",
    "submitted_qty",
    "quantity_unit",
    "lot_size",
    "slot_capital_cny",
    "maximum_reserved_cash_cny",
    "execution_mode",
)


class AuditAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


def _freeze(value: Any, field_name: str = "audit value") -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError(f"{field_name} must not contain NaN or infinity")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for raw_key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            key = str(raw_key or "").strip()
            if not key:
                raise ContractError(f"{field_name} contains a blank key")
            if key in frozen:
                raise ContractError(f"{field_name} contains duplicate normalized keys")
            frozen[key] = _freeze(item, f"{field_name}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item, f"{field_name}[]") for item in value)
    raise ContractError(f"{field_name} must be JSON-compatible")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def research_snapshot_sha256(payload: Mapping[str, Any]) -> str:
    """Hash the complete serialized audit envelope, excluding its own digest."""

    if not isinstance(payload, Mapping):
        raise ContractError("single-stock research payload must be a mapping")
    canonical = {
        str(key): _thaw(value)
        for key, value in payload.items()
        if str(key) != RESEARCH_SNAPSHOT_HASH_FIELD
    }
    try:
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractError("single-stock research payload is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _seal_research_payload(payload: dict[str, Any]) -> dict[str, Any]:
    payload[RESEARCH_SNAPSHOT_HASH_FIELD] = research_snapshot_sha256(payload)
    return payload


def _reason(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContractError(f"{field_name} must be non-empty")
    return text


@dataclass(frozen=True)
class AuditedValue:
    """A value with explicit missingness; genuine numeric zero stays data."""

    value: Any
    missing_reason: str | None = None

    def __post_init__(self) -> None:
        frozen = _freeze(self.value)
        object.__setattr__(self, "value", frozen)
        if frozen is None:
            object.__setattr__(
                self,
                "missing_reason",
                _reason(self.missing_reason, "missing_reason"),
            )
        elif self.missing_reason is not None:
            raise ContractError("missing_reason is only valid when value is None")

    @classmethod
    def missing(cls, reason: str) -> AuditedValue:
        return cls(None, missing_reason=reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": _thaw(self.value),
            "missing_reason": self.missing_reason,
        }


@dataclass(frozen=True)
class AuditSection:
    availability: AuditAvailability
    payload: Any
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if self.availability == AuditAvailability.AVAILABLE:
            if self.payload is None:
                raise ContractError("an AVAILABLE audit section requires payload")
            if self.unavailable_reason is not None:
                raise ContractError(
                    "unavailable_reason is invalid for an AVAILABLE audit section"
                )
            object.__setattr__(self, "payload", _freeze(self.payload, "section payload"))
            return
        if self.availability != AuditAvailability.UNAVAILABLE:
            raise ContractError("unsupported audit availability")
        if self.payload is not None:
            raise ContractError("an UNAVAILABLE audit section cannot expose payload")
        object.__setattr__(
            self,
            "unavailable_reason",
            _reason(self.unavailable_reason, "unavailable_reason"),
        )

    @classmethod
    def available(cls, payload: Any) -> AuditSection:
        return cls(AuditAvailability.AVAILABLE, payload)

    @classmethod
    def unavailable(cls, reason: str) -> AuditSection:
        return cls(AuditAvailability.UNAVAILABLE, None, unavailable_reason=reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "availability": self.availability.value,
            "payload": _thaw(self.payload),
            "unavailable_reason": self.unavailable_reason,
        }


def _audited_fields(
    values: Mapping[str, Any],
    fields: tuple[str, ...],
    *,
    namespace: str,
) -> dict[str, AuditedValue]:
    result: dict[str, AuditedValue] = {}
    for name in fields:
        if name not in values or values[name] is None:
            result[name] = AuditedValue.missing(f"{namespace}.{name} is missing")
        else:
            result[name] = AuditedValue(values[name])
    return result


def _candidate_analysis(
    candidate: Candidate,
    decision_date: str,
) -> AuditSection:
    features = candidate.features
    metrics = candidate.metrics
    if not isinstance(features, Mapping):
        return AuditSection.unavailable("candidate.features is not a mapping")
    if not isinstance(metrics, Mapping):
        return AuditSection.unavailable("candidate.metrics is not a mapping")

    raw_asof = features.get("feature_asof_date")
    if raw_asof in (None, ""):
        return AuditSection.unavailable("candidate feature_asof_date is missing")
    try:
        feature_asof = normalize_date(raw_asof, "candidate feature_asof_date")
    except ContractError as exc:
        return AuditSection.unavailable(str(exc))
    if feature_asof > decision_date:
        raise ContractError(
            "candidate feature_asof_date is later than decision_date; "
            "future evidence is forbidden"
        )
    if feature_asof != decision_date:
        return AuditSection.unavailable(
            "candidate feature_asof_date does not match the frozen D date"
        )

    candidate_values: dict[str, AuditedValue] = {
        "rank": (
            AuditedValue.missing("candidate.rank is missing")
            if candidate.rank is None
            else AuditedValue(candidate.rank)
        ),
        "action": (
            AuditedValue.missing("candidate.action is missing")
            if candidate.action in (None, "")
            else AuditedValue(candidate.action)
        ),
        "action_reason": (
            AuditedValue.missing("candidate.action_reason is missing")
            if candidate.action_reason in (None, "")
            else AuditedValue(candidate.action_reason)
        ),
    }
    feature_values = _audited_fields(
        features,
        CANDIDATE_FEATURE_AUDIT_FIELDS,
        namespace="candidate.features",
    )
    metric_values = _audited_fields(
        metrics,
        CANDIDATE_METRIC_AUDIT_FIELDS,
        namespace="candidate.metrics",
    )
    return AuditSection.available(
        {
            "candidate": {
                key: item.to_dict() for key, item in candidate_values.items()
            },
            "features": {
                key: item.to_dict() for key, item in feature_values.items()
            },
            "metrics": {
                key: item.to_dict() for key, item in metric_values.items()
            },
        }
    )


def _order_spec(candidate: Candidate, decision_date: str) -> AuditSection:
    spec = candidate.order_spec
    if not isinstance(spec, Mapping) or not spec:
        return AuditSection.unavailable("candidate frozen order_spec is missing")
    if spec.get("schema_version") != ORDER_SPEC_SCHEMA:
        return AuditSection.unavailable("candidate order_spec schema is unsupported")
    raw_date = spec.get("decision_date")
    if raw_date in (None, ""):
        return AuditSection.unavailable("candidate order_spec decision_date is missing")
    try:
        order_date = normalize_date(raw_date, "candidate order_spec decision_date")
    except ContractError as exc:
        return AuditSection.unavailable(str(exc))
    if order_date > decision_date:
        raise ContractError(
            "candidate order_spec decision_date is later than D; "
            "future evidence is forbidden"
        )
    if order_date != decision_date:
        return AuditSection.unavailable(
            "candidate order_spec decision_date does not match the frozen D date"
        )

    fields = _audited_fields(
        spec,
        ORDER_SPEC_AUDIT_FIELDS,
        namespace="candidate.order_spec",
    )
    return AuditSection.available(
        {key: item.to_dict() for key, item in fields.items()}
    )


def _lifecycle(
    lifecycle: LimitLifecycleSnapshot | None,
    decision_date: str,
) -> AuditSection:
    # Lifecycle is auxiliary research evidence only.  Every builder/coverage
    # failure becomes UNAVAILABLE and must never modify the V3 hard gate.
    if lifecycle is None:
        return AuditSection.unavailable("D-day limit lifecycle was not supplied")
    if not isinstance(lifecycle, LimitLifecycleSnapshot):
        return AuditSection.unavailable("D-day limit lifecycle has an unsupported type")
    if not lifecycle.valid:
        suffix = ",".join(lifecycle.invalid_reasons) or "unknown_validation_failure"
        return AuditSection.unavailable(f"limit lifecycle unavailable: {suffix}")
    if lifecycle.decision_date != decision_date:
        return AuditSection.unavailable(
            "limit lifecycle decision_date does not match the frozen D date"
        )
    return AuditSection.available(lifecycle.to_dict())


@dataclass(frozen=True)
class SingleStockResearchSnapshot:
    """Read-only research composition; never a ranking or execution input."""

    single_stock: SingleStockSnapshotV3
    candidate_analysis: AuditSection
    order_spec: AuditSection
    limit_lifecycle: AuditSection
    schema_version: str = field(init=False, default=SINGLE_STOCK_RESEARCH_SCHEMA)
    decision_effect: str = field(init=False, default=AUDIT_ONLY_EFFECT)

    def __post_init__(self) -> None:
        if not isinstance(self.single_stock, SingleStockSnapshotV3):
            raise ContractError("single_stock must be SingleStockSnapshotV3")
        for name in ("candidate_analysis", "order_spec", "limit_lifecycle"):
            if not isinstance(getattr(self, name), AuditSection):
                raise ContractError(f"{name} must be AuditSection")

    def to_dict(self) -> dict[str, Any]:
        return _seal_research_payload({
            "schema_version": self.schema_version,
            "decision_effect": self.decision_effect,
            "availability": AuditAvailability.AVAILABLE.value,
            "unavailable_reason": None,
            "decision_date": self.single_stock.decision_date,
            "decision_asof": self.single_stock.decision_asof,
            "ts_code": self.single_stock.ts_code,
            "single_stock": self.single_stock.to_dict(),
            "candidate_analysis": self.candidate_analysis.to_dict(),
            "order_spec": self.order_spec.to_dict(),
            "limit_lifecycle": self.limit_lifecycle.to_dict(),
        })


def build_single_stock_research_snapshot(
    candidate: Candidate,
    single_stock: SingleStockSnapshotV3,
    lifecycle: LimitLifecycleSnapshot | None = None,
) -> SingleStockResearchSnapshot:
    """Compose an immutable, D-only audit view without mutating any input."""

    if not isinstance(candidate, Candidate):
        raise ContractError("candidate must be Candidate")
    if not isinstance(single_stock, SingleStockSnapshotV3):
        raise ContractError("single_stock must be SingleStockSnapshotV3")
    if normalize_ts_code(candidate.ts_code) != single_stock.ts_code:
        raise ContractError("candidate and single_stock ts_code disagree")
    if dict(candidate.source_ranks) != dict(single_stock.source_ranks):
        raise ContractError("candidate and single_stock source ranks disagree")

    decision_date = single_stock.decision_date
    return SingleStockResearchSnapshot(
        single_stock=single_stock,
        candidate_analysis=_candidate_analysis(candidate, decision_date),
        order_spec=_order_spec(candidate, decision_date),
        limit_lifecycle=_lifecycle(lifecycle, decision_date),
    )


def unavailable_single_stock_research(
    candidate: Candidate,
    *,
    decision_date: str,
    decision_asof: str,
    reason: str,
) -> dict[str, Any]:
    """Return a typed failure envelope for a new signal's audit attachment."""

    if not isinstance(candidate, Candidate):
        raise ContractError("candidate must be Candidate")
    day = normalize_date(decision_date, "unavailable research decision_date")
    message = _reason(reason, "unavailable research reason")
    return _seal_research_payload({
        "schema_version": SINGLE_STOCK_RESEARCH_SCHEMA,
        "decision_effect": AUDIT_ONLY_EFFECT,
        "availability": AuditAvailability.UNAVAILABLE.value,
        "unavailable_reason": message,
        "decision_date": day,
        "decision_asof": str(decision_asof),
        "ts_code": normalize_ts_code(candidate.ts_code),
        "single_stock": None,
        "candidate_analysis": AuditSection.unavailable(message).to_dict(),
        "order_spec": AuditSection.unavailable(message).to_dict(),
        "limit_lifecycle": AuditSection.unavailable(message).to_dict(),
    })


__all__ = [
    "AUDIT_ONLY_EFFECT",
    "AuditAvailability",
    "AuditSection",
    "AuditedValue",
    "RESEARCH_SNAPSHOT_HASH_FIELD",
    "SINGLE_STOCK_RESEARCH_SCHEMA",
    "SingleStockResearchSnapshot",
    "build_single_stock_research_snapshot",
    "research_snapshot_sha256",
    "unavailable_single_stock_research",
]
