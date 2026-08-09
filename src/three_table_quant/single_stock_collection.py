from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .candidate_facts import candidate_validation_inputs
from .domain import Candidate, ContractError, normalize_date
from .limit_lifecycle import build_limit_lifecycle
from .limit_lifecycle import EXPECTED_SESSION_MINUTES
from .single_stock_minute_archive import (
    MinuteArchiveReference,
    canonicalize_minute_bars,
    minute_bars_content_sha256,
)
from .single_stock_research import build_single_stock_research_snapshot
from .security_master import PointInTimeSecurityMaster, SecurityMasterResolution
from .single_stock_v3 import (
    BOARD_LOT_FIELD,
    D_CLOSE_FIELD,
    DELISTING_FIELD,
    MAX_ORDER_SHARES_FIELD,
    PRICE_TICK_FIELD,
    PRICING_VERIFIED_FIELD,
    SECURITY_BOARD_FIELD,
    SECURITY_PRICE_LIMIT_PCT_FIELD,
    SECURITY_PRICE_TICK_FIELD,
    ST_FIELD,
    SUSPENDED_FIELD,
    TRADING_RULES_VERIFIED_FIELD,
    FactProvenance,
    SingleStockFact,
    SingleStockSnapshotV3,
)


CLASSIFICATION_INDUSTRY_FIELD = "classification.industry"
CLASSIFICATION_STAGE_FIELD = "classification.stage_transition"
LIMIT_UP_PRICE_FIELD = "market.limit_up_price"
LIMIT_UP_MECHANISM_FIELD = "market.limit_up_mechanism_pct"
LIFECYCLE_INPUT_FIELD = "research.limit_lifecycle_input"


def _digest(value: Any) -> str:
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _provenance(
    *,
    provider: str,
    dataset_version: str,
    decision_asof: str,
    payload: Any,
) -> FactProvenance:
    # ``decision_asof`` is intentionally used as the conservative known time:
    # the source rows/configuration were available no later than the atomic
    # signal freeze, and claiming an earlier time without provider evidence
    # would weaken the point-in-time contract.
    return FactProvenance(
        provider=provider,
        dataset_version=dataset_version,
        known_at=decision_asof,
        fetched_at=decision_asof,
        content_sha256=_digest(payload),
    )


def _fact_or_missing(
    value: Any,
    provenance: FactProvenance,
    reason: str,
    *,
    unit: str | None = None,
) -> SingleStockFact:
    if value is None:
        return SingleStockFact.missing(reason, provenance, unit=unit)
    return SingleStockFact(value, provenance, unit=unit)


def _security_provenance(resolution: SecurityMasterResolution) -> FactProvenance:
    return FactProvenance(
        provider=resolution.provider,
        dataset_version=resolution.dataset_version,
        known_at=resolution.known_at,
        fetched_at=resolution.fetched_at,
        content_sha256=resolution.content_sha256,
        revision_id=resolution.revision_id,
        source_uri=resolution.source_uri,
    )


def build_candidate_single_stock_research(
    candidate: Candidate,
    *,
    decision_date: str,
    decision_asof: str,
    execution: dict[str, Any],
    security_master: PointInTimeSecurityMaster,
    minute_bars: Sequence[Any] | None,
    minute_fetch_failed: bool = False,
    minute_artifact: MinuteArchiveReference | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze P0/P1 single-stock evidence without affecting the decision path.

    The caller must invoke this only after ranking and ``order_spec`` have been
    frozen.  The returned object is additive audit evidence; it is never read by
    feature generation, ranking, policy gating, or ledger execution.
    """

    day = normalize_date(decision_date, "single-stock collection decision_date")
    if not isinstance(candidate, Candidate):
        raise ContractError("single-stock collection candidate must be Candidate")
    facts = candidate_validation_inputs(candidate)

    source_payload = {
        "source_ranks": candidate.source_ranks,
        "source_values": candidate.source_values,
    }
    source_provenance = _provenance(
        provider="FROZEN_THREE_SOURCE_ROWS",
        dataset_version="candidate_facts_v1",
        decision_asof=decision_asof,
        payload=source_payload,
    )
    policy_payload = {
        "price_tick": execution.get("price_tick"),
        "lot_size": execution.get("lot_size"),
        "order_spec": candidate.order_spec,
    }
    policy_provenance = _provenance(
        provider="SYSTEM_POLICY",
        dataset_version="system_config_v1",
        decision_asof=decision_asof,
        payload=policy_payload,
    )
    if not isinstance(security_master, PointInTimeSecurityMaster):
        raise ContractError("point-in-time security master is required")
    security = security_master.resolve(
        candidate.ts_code,
        decision_date=day,
        decision_asof=decision_asof,
    )
    security_provenance = _security_provenance(security)
    canonical_bars = (
        canonicalize_minute_bars(minute_bars, decision_date=day)
        if minute_bars is not None
        else []
    )
    bars_content_sha256 = (
        minute_bars_content_sha256(canonical_bars)
        if minute_bars is not None
        else None
    )
    bars_payload = {
        "ts_code": candidate.ts_code,
        "decision_date": day,
        "fetch_failed": bool(minute_fetch_failed),
        "bar_count": None if minute_bars is None else len(minute_bars),
        "bars_content_sha256": bars_content_sha256,
        "providers": sorted(
            {
                str(getattr(bar, "provider", "UNSPECIFIED")).upper()
                for bar in (minute_bars or ())
            }
        ),
    }
    if minute_artifact is not None:
        if minute_bars is None:
            raise ContractError(
                "minute artifact reference requires the matching in-memory bars"
            )
        parsed_artifact = (
            minute_artifact
            if isinstance(minute_artifact, MinuteArchiveReference)
            else MinuteArchiveReference.from_dict(minute_artifact)
        )
        parsed_artifact.validate_for(
            ts_code=candidate.ts_code,
            decision_date=day,
            decision_asof=decision_asof,
            bars_content_sha256=bars_content_sha256,
            bar_count=len(canonical_bars),
            full_session=(
                tuple(item["time"] for item in canonical_bars)
                == EXPECTED_SESSION_MINUTES
            ),
        )
        observed_providers = tuple(
            sorted({item["provider"] for item in canonical_bars})
        )
        if parsed_artifact.providers != observed_providers:
            raise ContractError("minute artifact providers mismatch in research snapshot")
        bars_payload["minute_artifact"] = parsed_artifact.to_dict()
    minute_provenance = _provenance(
        provider="MARKET_MINUTE_ADAPTER",
        dataset_version="d_day_minute_close_proxy_v1",
        decision_asof=decision_asof,
        payload={"summary": bars_payload, "bars": canonical_bars},
    )

    d_close = facts.get("d_close")
    policy_price_tick = execution.get("price_tick")
    security_price_tick = security.value("price_tick")
    lifecycle_price_tick = (
        security_price_tick if security_price_tick is not None else policy_price_tick
    )
    board_lot = execution.get("lot_size")
    submitted_qty = (
        candidate.order_spec.get("submitted_qty")
        if isinstance(candidate.order_spec, dict)
        else None
    )
    limit_price = facts.get("limit_up_price")
    frozen_limit_pct = facts.get("mechanism_limit_pct")
    security_limit_pct = security.value("price_limit_pct")
    rules_verified = security.value("trading_rules_verified")
    pricing_inputs_complete = all(
        value is not None
        for value in (
            d_close,
            limit_price,
            frozen_limit_pct,
            security_limit_pct,
            security_price_tick,
            rules_verified,
        )
    )
    pricing_verified = None
    if pricing_inputs_complete:
        pricing_verified = bool(
            rules_verified is True
            and abs(float(frozen_limit_pct) - float(security_limit_pct)) <= 1e-9
        )
    pricing_provenance = _provenance(
        provider="FROZEN_SOURCE_AND_POINT_IN_TIME_SECURITY_MASTER",
        dataset_version="pricing_cross_check_v1",
        decision_asof=decision_asof,
        payload={
            "source_sha256": source_provenance.content_sha256,
            "security_master_sha256": security.content_sha256,
            "record_id": security.record_id,
            "d_close": d_close,
            "limit_price": limit_price,
            "frozen_limit_pct": frozen_limit_pct,
            "security_limit_pct": security_limit_pct,
            "security_price_tick": security_price_tick,
            "trading_rules_verified": rules_verified,
        },
    )
    fact_map = {
        # Every value comes from an effective-dated, as-of-safe record.  The
        # bootstrap master is intentionally empty: absence is UNKNOWN and is
        # never converted to False/PASS from the code, name, board or policy.
        SUSPENDED_FIELD: _fact_or_missing(
            security.value("is_suspended"),
            security_provenance,
            security.missing_reason("is_suspended")
            or "POINT_IN_TIME_SUSPENSION_STATUS_UNAVAILABLE",
        ),
        ST_FIELD: _fact_or_missing(
            security.value("is_st"),
            security_provenance,
            security.missing_reason("is_st")
            or "POINT_IN_TIME_ST_STATUS_UNAVAILABLE",
        ),
        DELISTING_FIELD: _fact_or_missing(
            security.value("is_delisting_period"),
            security_provenance,
            security.missing_reason("is_delisting_period")
            or "POINT_IN_TIME_DELISTING_STATUS_UNAVAILABLE",
        ),
        TRADING_RULES_VERIFIED_FIELD: _fact_or_missing(
            rules_verified,
            security_provenance,
            security.missing_reason("trading_rules_verified")
            or "EXCHANGE_SECURITY_RULES_UNAVAILABLE",
        ),
        SECURITY_BOARD_FIELD: _fact_or_missing(
            security.value("board"),
            security_provenance,
            security.missing_reason("board") or "SECURITY_BOARD_UNAVAILABLE",
        ),
        SECURITY_PRICE_LIMIT_PCT_FIELD: _fact_or_missing(
            security_limit_pct,
            security_provenance,
            security.missing_reason("price_limit_pct")
            or "SECURITY_PRICE_LIMIT_UNAVAILABLE",
            unit="PERCENT",
        ),
        SECURITY_PRICE_TICK_FIELD: _fact_or_missing(
            security_price_tick,
            security_provenance,
            security.missing_reason("price_tick")
            or "POINT_IN_TIME_PRICE_TICK_UNAVAILABLE",
            unit="CNY_PER_SHARE",
        ),
        PRICING_VERIFIED_FIELD: _fact_or_missing(
            pricing_verified,
            pricing_provenance,
            "POINT_IN_TIME_PRICING_CROSS_CHECK_INCOMPLETE",
        ),
        D_CLOSE_FIELD: _fact_or_missing(
            d_close,
            source_provenance,
            "FROZEN_D_CLOSE_UNAVAILABLE",
            unit="CNY_PER_SHARE",
        ),
        PRICE_TICK_FIELD: _fact_or_missing(
            policy_price_tick,
            policy_provenance,
            "PRICE_TICK_POLICY_UNAVAILABLE",
            unit="CNY_PER_SHARE",
        ),
        BOARD_LOT_FIELD: _fact_or_missing(
            board_lot,
            policy_provenance,
            "BOARD_LOT_POLICY_UNAVAILABLE",
            unit="SHARES",
        ),
        MAX_ORDER_SHARES_FIELD: _fact_or_missing(
            submitted_qty,
            policy_provenance,
            "FROZEN_ORDER_CAPACITY_UNAVAILABLE",
            unit="SHARES",
        ),
        CLASSIFICATION_INDUSTRY_FIELD: _fact_or_missing(
            facts.get("industry"),
            source_provenance,
            "INDUSTRY_UNAVAILABLE",
        ),
        CLASSIFICATION_STAGE_FIELD: _fact_or_missing(
            facts.get("stage_transition"),
            source_provenance,
            "STAGE_TRANSITION_UNAVAILABLE",
        ),
        LIMIT_UP_PRICE_FIELD: _fact_or_missing(
            limit_price,
            source_provenance,
            "FROZEN_LIMIT_UP_PRICE_UNAVAILABLE",
            unit="CNY_PER_SHARE",
        ),
        LIMIT_UP_MECHANISM_FIELD: _fact_or_missing(
            facts.get("mechanism_limit_pct"),
            source_provenance,
            "LIMIT_UP_MECHANISM_UNAVAILABLE",
            unit="PERCENT",
        ),
        LIFECYCLE_INPUT_FIELD: (
            SingleStockFact.missing(
                (
                    "MINUTE_FETCH_FAILED"
                    if minute_fetch_failed
                    else "MINUTE_COLLECTION_NOT_ATTEMPTED"
                ),
                minute_provenance,
            )
            if minute_bars is None
            else SingleStockFact(bars_payload, minute_provenance)
        ),
    }
    single_stock = SingleStockSnapshotV3(
        ts_code=candidate.ts_code,
        name=candidate.name,
        decision_date=day,
        decision_asof=decision_asof,
        source_ranks=candidate.source_ranks,
        facts=fact_map,
    )

    lifecycle = None
    if minute_bars is not None:
        lifecycle = build_limit_lifecycle(
            minute_bars,
            day,
            float(limit_price) if limit_price is not None else 0.0,
            float(lifecycle_price_tick) if lifecycle_price_tick is not None else 0.0,
        )
    return build_single_stock_research_snapshot(
        candidate,
        single_stock,
        lifecycle,
    ).to_dict()


__all__ = [
    "CLASSIFICATION_INDUSTRY_FIELD",
    "CLASSIFICATION_STAGE_FIELD",
    "LIFECYCLE_INPUT_FIELD",
    "LIMIT_UP_MECHANISM_FIELD",
    "LIMIT_UP_PRICE_FIELD",
    "build_candidate_single_stock_research",
]
