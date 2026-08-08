from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from .candidate_facts import candidate_validation_inputs
from .domain import Candidate, ContractError, normalize_date
from .limit_lifecycle import build_limit_lifecycle
from .single_stock_research import build_single_stock_research_snapshot
from .single_stock_v3 import (
    BOARD_LOT_FIELD,
    D_CLOSE_FIELD,
    DELISTING_FIELD,
    MAX_ORDER_SHARES_FIELD,
    PRICE_TICK_FIELD,
    PRICING_VERIFIED_FIELD,
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


def _canonical_minute_bars(bars: Sequence[Any] | None) -> list[dict[str, Any]]:
    if bars is None:
        return []
    return [
        {
            "date": getattr(bar, "date", None),
            "time": getattr(bar, "time", None),
            "open": getattr(bar, "open", None),
            "close": getattr(bar, "close", None),
            "high": getattr(bar, "high", None),
            "low": getattr(bar, "low", None),
            "volume": getattr(bar, "volume", None),
            "amount": getattr(bar, "amount", None),
            "volume_unit": getattr(bar, "volume_unit", None),
            "price_tick": getattr(bar, "price_tick", None),
            "source_time": getattr(bar, "source_time", None),
            "time_semantics": getattr(bar, "time_semantics", None),
            "provider": getattr(bar, "provider", None),
            "price_adjustment": getattr(bar, "price_adjustment", None),
        }
        for bar in bars
    ]


def build_candidate_single_stock_research(
    candidate: Candidate,
    *,
    decision_date: str,
    decision_asof: str,
    execution: dict[str, Any],
    minute_bars: Sequence[Any] | None,
    minute_fetch_failed: bool = False,
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
    security_gap_provenance = _provenance(
        provider="SECURITY_MASTER_NOT_CONNECTED",
        dataset_version="security_master_gap_v1",
        decision_asof=decision_asof,
        payload={
            "ts_code": candidate.ts_code,
            "missing": ["suspension", "delisting", "trading_rules"],
        },
    )
    canonical_bars = _canonical_minute_bars(minute_bars)
    bars_content_sha256 = _digest(canonical_bars) if minute_bars is not None else None
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
    minute_provenance = _provenance(
        provider="MARKET_MINUTE_ADAPTER",
        dataset_version="d_day_minute_close_proxy_v1",
        decision_asof=decision_asof,
        payload={"summary": bars_payload, "bars": canonical_bars},
    )

    d_close = facts.get("d_close")
    price_tick = execution.get("price_tick")
    board_lot = execution.get("lot_size")
    submitted_qty = (
        candidate.order_spec.get("submitted_qty")
        if isinstance(candidate.order_spec, dict)
        else None
    )
    limit_price = facts.get("limit_up_price")
    fact_map = {
        # A point-in-time security master is not available in this repository.
        # Preserve the gap as UNKNOWN; never infer False from a missing flag.
        SUSPENDED_FIELD: SingleStockFact.missing(
            "POINT_IN_TIME_SUSPENSION_STATUS_UNAVAILABLE",
            security_gap_provenance,
        ),
        DELISTING_FIELD: SingleStockFact.missing(
            "POINT_IN_TIME_DELISTING_STATUS_UNAVAILABLE",
            security_gap_provenance,
        ),
        TRADING_RULES_VERIFIED_FIELD: SingleStockFact.missing(
            "EXCHANGE_SECURITY_RULES_NOT_CONNECTED",
            security_gap_provenance,
        ),
        PRICING_VERIFIED_FIELD: _fact_or_missing(
            (
                True
                if d_close is not None and limit_price is not None
                else None
            ),
            source_provenance,
            "FROZEN_PRICING_EVIDENCE_INCOMPLETE",
        ),
        D_CLOSE_FIELD: _fact_or_missing(
            d_close,
            source_provenance,
            "FROZEN_D_CLOSE_UNAVAILABLE",
            unit="CNY_PER_SHARE",
        ),
        PRICE_TICK_FIELD: _fact_or_missing(
            price_tick,
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
            float(price_tick) if price_tick is not None else 0.0,
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
