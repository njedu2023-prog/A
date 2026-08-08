from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .domain import ContractError, normalize_date, normalize_ts_code
from .sources import SOURCE_IDS


BENCHMARK_SCHEMA_VERSION = "batch_benchmarks_v1"
RETURN_BASIS = "net_return_on_allocated"


def _finite_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{field_name} must be a finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{field_name} must be a finite number") from exc
    if not math.isfinite(parsed):
        raise ContractError(f"{field_name} must be a finite number")
    return parsed


def _positive_rank(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContractError(f"{field_name} must be a positive integer")
    return value


def _source_depths(
    source_table_sizes: Mapping[str, Any] | None,
) -> dict[str, int]:
    if not isinstance(source_table_sizes, Mapping):
        raise ContractError(
            "legacy Borda recovery requires frozen source table depths"
        )
    depths: dict[str, int] = {}
    for source_id in SOURCE_IDS:
        if source_id not in source_table_sizes:
            raise ContractError(
                f"legacy Borda recovery is missing {source_id} table depth"
            )
        depths[source_id] = _positive_rank(
            source_table_sizes[source_id],
            f"{source_id} table depth",
        )
    return depths


def _borda_score(
    candidate: Mapping[str, Any],
    source_table_sizes: Mapping[str, Any] | None,
) -> float:
    features = candidate.get("features")
    if isinstance(features, Mapping) and features.get("rank_borda") is not None:
        return _finite_number(features.get("rank_borda"), "candidate rank_borda")

    # Frozen V1 signals predate the persisted normalized Borda feature, but do
    # retain the three immutable source ranks and frozen source depths. Recover
    # exactly the same score as features._rank_features; never assume that all
    # three source tables had the same display depth.
    depths = _source_depths(source_table_sizes)
    source_ranks = candidate.get("source_ranks")
    if not isinstance(source_ranks, Mapping):
        raise ContractError("legacy Borda recovery requires immutable source ranks")
    points = 0.0
    denominator = 0.0
    for source_id in SOURCE_IDS:
        if source_id not in source_ranks:
            raise ContractError(
                f"legacy Borda recovery is missing {source_id} candidate rank"
            )
        rank = _positive_rank(
            source_ranks[source_id],
            f"{source_id} candidate rank",
        )
        size = depths[source_id]
        if rank > size:
            raise ContractError(
                f"{source_id} candidate rank {rank} exceeds frozen depth {size}"
            )
        points += size - rank + 1
        denominator += size
    return points / denominator


def _normalize_candidates(
    candidates: Sequence[Mapping[str, Any]],
    source_table_sizes: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    codes: set[str] = set()
    ranks: set[int] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise ContractError("candidate must be an object")
        code = normalize_ts_code(candidate.get("ts_code"))
        rank = _positive_rank(candidate.get("rank"), "candidate rank")
        if code in codes:
            raise ContractError(f"duplicate candidate code: {code}")
        if rank in ranks:
            raise ContractError(f"duplicate model rank: {rank}")
        codes.add(code)
        ranks.add(rank)
        normalized.append(
            {
                "ts_code": code,
                "name": str(candidate.get("name") or ""),
                "model_rank": rank,
                "borda_score": _borda_score(candidate, source_table_sizes),
            }
        )

    expected_ranks = list(range(1, len(normalized) + 1))
    observed_ranks = sorted(ranks)
    if observed_ranks != expected_ranks:
        raise ContractError(
            "model ranks must be contiguous from 1 within the frozen cohort"
        )
    return normalized


def _normalize_outcomes(
    candidates: Sequence[Mapping[str, Any]],
    outcomes: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not isinstance(outcomes, Mapping):
        raise ContractError("outcomes must be keyed by candidate code")
    normalized: dict[str, dict[str, Any]] = {}
    for raw_code, outcome in outcomes.items():
        code = normalize_ts_code(raw_code)
        if code in normalized:
            raise ContractError(f"duplicate outcome code: {code}")
        if not isinstance(outcome, Mapping):
            raise ContractError(f"outcome for {code} must be an object")
        is_final = outcome.get("is_final")
        if not isinstance(is_final, bool):
            raise ContractError(f"outcome is_final for {code} must be boolean")
        raw_return = outcome.get(RETURN_BASIS)
        if is_final:
            net_return = _finite_number(raw_return, f"{code} {RETURN_BASIS}")
        else:
            if raw_return is not None:
                raise ContractError(
                    f"pending outcome for {code} must keep {RETURN_BASIS} null"
                )
            net_return = None
        normalized[code] = {
            "is_final": is_final,
            RETURN_BASIS: net_return,
        }

    candidate_codes = {str(item["ts_code"]) for item in candidates}
    if set(normalized) != candidate_codes:
        missing = sorted(candidate_codes - set(normalized))
        extra = sorted(set(normalized) - candidate_codes)
        raise ContractError(
            f"outcome cohort mismatch; missing={missing}, extra={extra}"
        )
    return normalized


def _policy_result(
    policy_id: str,
    label: str,
    selected: Sequence[Mapping[str, Any]],
    outcomes: Mapping[str, Mapping[str, Any]],
    *,
    denominator_slots: int,
) -> dict[str, Any]:
    if denominator_slots <= 0 or len(selected) > denominator_slots:
        raise ContractError("benchmark denominator is inconsistent with selection")
    constituents: list[dict[str, Any]] = []
    pending_count = 0
    total = 0.0
    for candidate in selected:
        code = str(candidate["ts_code"])
        outcome = outcomes[code]
        is_final = bool(outcome["is_final"])
        value = outcome[RETURN_BASIS]
        if is_final:
            total += float(value)
        else:
            pending_count += 1
        constituents.append(
            {
                "ts_code": code,
                "name": candidate["name"],
                "model_rank": candidate["model_rank"],
                "borda_rank": candidate.get("borda_rank"),
                "borda_score": candidate["borda_score"],
                "is_final": is_final,
                RETURN_BASIS: value,
            }
        )

    is_final = pending_count == 0
    return {
        "policy_id": policy_id,
        "label": label,
        "return_basis": RETURN_BASIS,
        "denominator_slots": denominator_slots,
        "selected_count": len(selected),
        "cash_slot_count": denominator_slots - len(selected),
        "final_count": len(selected) - pending_count,
        "pending_count": pending_count,
        "is_final": is_final,
        "portfolio_return": total / denominator_slots if is_final else None,
        "constituents": constituents,
    }


def build_batch_benchmarks(
    decision_date: str,
    candidates: Sequence[Mapping[str, Any]],
    outcomes: Mapping[str, Mapping[str, Any]],
    *,
    fixed_ranks: Sequence[int] = (1, 2, 3),
    comparison_depths: Sequence[int] = (1, 2, 3),
    source_table_sizes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build same-cohort counterfactual policies for one frozen D-day batch.

    Every non-cash policy consumes the same ``net_return_on_allocated`` label.
    Top-k policies retain exactly k capital slots; unavailable slots stay in
    cash rather than increasing the weight of the remaining candidates.
    A policy remains pending until every selected candidate has a final label.
    """

    normalized_date = normalize_date(decision_date, "benchmark decision_date")
    cohort = _normalize_candidates(candidates, source_table_sizes)
    normalized_outcomes = _normalize_outcomes(cohort, outcomes)

    normalized_fixed = tuple(
        _positive_rank(rank, "fixed rank") for rank in fixed_ranks
    )
    normalized_depths = tuple(
        _positive_rank(depth, "comparison depth") for depth in comparison_depths
    )
    if len(set(normalized_fixed)) != len(normalized_fixed):
        raise ContractError("fixed ranks must be unique")
    if len(set(normalized_depths)) != len(normalized_depths):
        raise ContractError("comparison depths must be unique")

    model_order = sorted(cohort, key=lambda item: item["model_rank"])
    borda_order = sorted(
        cohort,
        key=lambda item: (
            -float(item["borda_score"]),
            int(item["model_rank"]),
            str(item["ts_code"]),
        ),
    )
    borda_rank = {
        str(item["ts_code"]): index
        for index, item in enumerate(borda_order, start=1)
    }
    for item in cohort:
        item["borda_rank"] = borda_rank[str(item["ts_code"])]

    policies: dict[str, dict[str, Any]] = {}
    policies["cash"] = _policy_result(
        "cash",
        "现金",
        [],
        normalized_outcomes,
        denominator_slots=1,
    )
    all_slots = max(1, len(cohort))
    policies["all_candidates_equal_weight"] = _policy_result(
        "all_candidates_equal_weight",
        "全部候选等权",
        model_order,
        normalized_outcomes,
        denominator_slots=all_slots,
    )

    by_model_rank = {int(item["model_rank"]): item for item in cohort}
    for rank in normalized_fixed:
        selected = [by_model_rank[rank]] if rank in by_model_rank else []
        policy_id = f"fixed_model_rank_{rank}"
        policies[policy_id] = _policy_result(
            policy_id,
            f"固定TOP{rank}",
            selected,
            normalized_outcomes,
            denominator_slots=1,
        )

    for depth in normalized_depths:
        for order_id, label, ordered in (
            ("model", "模型序", model_order),
            ("borda", "Borda序", borda_order),
        ):
            policy_id = f"{order_id}_top{depth}_equal_weight"
            policies[policy_id] = _policy_result(
                policy_id,
                f"{label}TOP{depth}等权",
                ordered[:depth],
                normalized_outcomes,
                denominator_slots=depth,
            )

    return {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "decision_date": normalized_date,
        "return_unit": "decimal",
        "return_basis": RETURN_BASIS,
        "cohort_count": len(cohort),
        "cohort_final_count": sum(
            bool(item["is_final"]) for item in normalized_outcomes.values()
        ),
        "is_final": all(
            bool(item["is_final"]) for item in normalized_outcomes.values()
        ),
        "model_order": [str(item["ts_code"]) for item in model_order],
        "borda_order": [str(item["ts_code"]) for item in borda_order],
        "policies": policies,
    }
