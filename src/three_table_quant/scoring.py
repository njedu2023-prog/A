from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .domain import Candidate, normalize_date
from .features import build_feature_snapshot
from .ranking_engine import (
    ArtifactValidationError,
    LearnedChallenger,
    rank_with_champion,
    rank_with_learned,
)
from .sources import SOURCE_A, SOURCE_PREMIUM


def _load_resolved_artifact(
    resolved_model: dict[str, Any] | None,
    model_base_dir: str | Path | None,
) -> tuple[Any, str | None]:
    """Load a checksum-resolved JSON artifact from its contained repo path.

    ``resolve_champion`` has already checked the artifact checksum.  This
    second contained-path check keeps scoring safe when called directly and
    avoids ever importing or unpickling executable model payloads.
    """

    if not isinstance(resolved_model, dict) or resolved_model.get("fallback") is True:
        return None, None
    model = resolved_model.get("model")
    if not isinstance(model, dict) or str(model.get("kind") or "").upper() != "TRAINED":
        return None, None
    relative_path = Path(str(model.get("artifact_path") or ""))
    if not relative_path.parts or relative_path.is_absolute() or ".." in relative_path.parts:
        return None, "LEARNED_ARTIFACT_PATH_INVALID"
    if model_base_dir is None:
        return None, "LEARNED_ARTIFACT_BASE_DIR_UNAVAILABLE"
    base = Path(model_base_dir).resolve()
    resolved = (base / relative_path).resolve()
    if resolved != base and base not in resolved.parents:
        return None, "LEARNED_ARTIFACT_PATH_INVALID"
    try:
        if resolved.stat().st_size > 5 * 1024 * 1024:
            return None, "LEARNED_ARTIFACT_TOO_LARGE"
        with resolved.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "LEARNED_ARTIFACT_UNAVAILABLE"
    if not isinstance(payload, dict):
        return None, "LEARNED_ARTIFACT_INVALID"
    return payload, None


def _candidate_decision_date(candidate: Candidate) -> str | None:
    """Infer D from frozen source rows while keeping legacy unit tests valid."""

    raw_values = [
        candidate.source_values.get(SOURCE_A, {}).get("trade_date"),
        candidate.source_values.get(SOURCE_PREMIUM, {}).get("trade_date"),
    ]
    parsed: set[str] = set()
    for value in raw_values:
        if value in (None, ""):
            continue
        try:
            parsed.add(normalize_date(value, "candidate decision_date"))
        except Exception:
            return None
    return next(iter(parsed)) if len(parsed) == 1 else None


def build_features(
    candidate: Candidate,
    bars: list[Any],
    table_sizes: dict[str, int],
    *,
    decision_date: str | None = None,
    min_daily_bars: int = 21,
) -> dict[str, Any]:
    snapshot = build_feature_snapshot(
        candidate,
        bars,
        table_sizes,
        decision_date=decision_date,
        min_daily_bars=min_daily_bars,
    )
    return snapshot.to_dict()


def estimate_round_trip_rate(execution: dict[str, Any]) -> float:
    """Estimate costs on the same basis used by the shadow ledger.

    Entry starts from an *actual* 09:25 fill, so there is no second synthetic
    entry-slippage deduction.  The buy is one order and the planned exit is one
    child order per configured minute; minimum commissions therefore matter at
    the slot level instead of being approximated by ``2 * commission_rate``.
    """

    capital = max(float(execution["slot_capital_cny"]), 1.0)
    commission_rate = float(execution["commission_rate"])
    minimum_commission = float(execution["minimum_commission_cny"])
    child_count = max(1, int(execution["exit_minute_count"]))
    buy_commission = max(minimum_commission, capital * commission_rate)
    child_amount = capital / child_count
    sell_commission = child_count * max(minimum_commission, child_amount * commission_rate)
    return (
        (buy_commission + sell_commission) / capital
        + float(execution["stamp_duty_sell_rate"])
        + 2.0 * float(execution["transfer_fee_rate_each_side"])
        + float(execution["slippage_rate_each_side"])
    )


def score_candidates(
    candidates: list[Candidate],
    bars_by_code: dict[str, list[Any]],
    table_sizes: dict[str, int],
    config: dict[str, Any],
    decision_date: str | None = None,
    resolved_model: dict[str, Any] | None = None,
    artifact: Any = None,
    model_base_dir: str | Path | None = None,
) -> list[Candidate]:
    """Apply the V2 formal output contract while preserving legacy metrics.

    All strict-intersection candidates remain SHADOW observations.  The policy
    gate is a separate diagnostic and never creates a broker order.
    """

    if not candidates:
        return []
    ranking = config["ranking"]
    execution = config["execution"]
    min_daily_bars = int(ranking.get("min_daily_bars", 21))
    effective_dates: set[str] = set()
    for candidate in candidates:
        effective_date = decision_date or _candidate_decision_date(candidate)
        if effective_date is not None:
            effective_dates.add(effective_date)
        candidate.features = build_features(
            candidate,
            bars_by_code.get(candidate.ts_code, []),
            table_sizes,
            decision_date=effective_date,
            min_daily_bars=min_daily_bars,
        )

    cost_rate = estimate_round_trip_rate(execution)
    cohort_date = next(iter(effective_dates)) if len(effective_dates) == 1 else None
    resolution_fallback = False
    resolution_reason: str | None = None
    metadata = resolved_model or {}
    resolved_payload = metadata.get("model") if isinstance(metadata.get("model"), dict) else metadata
    resolved_payload = resolved_payload if isinstance(resolved_payload, dict) else {}
    resolved_fallback = metadata.get("fallback") is True
    resolved_kind = str(resolved_payload.get("kind") or "").upper()
    expected_model_id = str(resolved_payload.get("model_id") or "").strip() or None
    artifact_load_reason: str | None = None
    if artifact is None and resolved_kind == "TRAINED" and not resolved_fallback:
        artifact, artifact_load_reason = _load_resolved_artifact(
            resolved_model,
            model_base_dir,
        )
    use_learned = artifact is not None and (
        resolved_model is None or (not resolved_fallback and resolved_kind == "TRAINED")
    )
    if resolved_fallback:
        resolution_fallback = True
        resolution_reason = str(metadata.get("fallback_reason") or "RESOLVED_MODEL_FALLBACK")
    elif resolved_kind == "TRAINED" and artifact is None:
        resolution_fallback = True
        resolution_reason = artifact_load_reason or "LEARNED_ARTIFACT_UNAVAILABLE"
    elif artifact is not None and resolved_model is not None and resolved_kind != "TRAINED":
        resolution_fallback = True
        resolution_reason = "RESOLVED_MODEL_NOT_TRAINED"

    ranked: list[tuple[Candidate, Any]]
    if use_learned:
        try:
            challenger = LearnedChallenger(
                artifact,
                ranking,
                estimated_round_trip_rate=cost_rate,
                expected_model_id=expected_model_id,
            )
            if cohort_date is None:
                raise ArtifactValidationError("learned inference requires one aligned decision_date")
            ranked = rank_with_learned(
                candidates,
                ranking,
                challenger,
                decision_date=cohort_date,
            )
        except (ArtifactValidationError, OSError, ValueError) as exc:
            resolution_fallback = True
            resolution_reason = f"LEARNED_ARTIFACT_REJECTED:{exc}"
            ranked = rank_with_champion(
                candidates,
                ranking,
                estimated_round_trip_rate=cost_rate,
            )
    else:
        ranked = rank_with_champion(
            candidates,
            ranking,
            estimated_round_trip_rate=cost_rate,
        )
    result: list[Candidate] = []
    for rank, (candidate, prediction) in enumerate(ranked, start=1):
        candidate.rank = rank
        prediction_payload = prediction.to_dict()
        historical_cvar = candidate.features.get("cvar_loss_10pct")
        # Explicit None checks are intentional: a genuine zero tail loss,
        # drawdown or utility is data, not a missing-value sentinel.
        cvar_loss = (
            prediction.expected_shortfall
            if historical_cvar is None
            else float(historical_cvar)
        )
        coverage = float(candidate.features.get("feature_coverage", 0.0))
        missing_fraction = max(0.0, min(1.0, 1.0 - coverage))
        eligible = prediction.gate_decision == "TRADE"
        candidate.metrics = {
            # Backward-compatible keys consumed by the current dashboard.
            "p_fill_0925": prediction.p_fill,
            "expected_gross_return": prediction.conditional_net_return_mean + cost_rate,
            "expected_net_return": prediction.conditional_net_return_mean,
            "cvar_loss_10pct": cvar_loss,
            "p_exit_delay": prediction.p_exit_delay,
            "uncertainty": prediction.uncertainty,
            "estimated_round_trip_rate": cost_rate,
            "utility_score": prediction.utility,
            "missing_fraction": missing_fraction,
            "policy_trade_eligible": eligible,
            # Formal V2 output heads.
            "prediction": prediction_payload,
            "model_id": prediction.model_id,
            "model_stage": prediction.model_stage,
            "feature_schema_version": prediction.feature_schema_version,
            "prediction_schema_version": prediction.schema_version,
            "expected_fill_ratio": prediction.expected_fill_ratio,
            "conditional_net_return_mean": prediction.conditional_net_return_mean,
            "conditional_net_return_q10": prediction.conditional_net_return_q10,
            "conditional_net_return_q50": prediction.conditional_net_return_q50,
            "conditional_net_return_q90": prediction.conditional_net_return_q90,
            "expected_shortfall_10pct": prediction.expected_shortfall,
            "expected_delay_days": prediction.expected_delay_days,
            "p_promotion": prediction.p_promotion,
            "gate_decision": prediction.gate_decision,
            "gate_reasons": list(prediction.gate_reasons),
            "ranking_fallback": prediction.ranking_fallback,
            "model_resolution_fallback": resolution_fallback,
            "model_resolution_reason": resolution_reason,
        }
        candidate.action = "SHADOW"
        candidate.policy_decision = {
            "trade_eligible": eligible,
            "gate_decision": prediction.gate_decision,
            "gate_reasons": list(prediction.gate_reasons),
            "model_id": prediction.model_id,
            "broker_order_created": False,
        }
        gate = "TRADE" if eligible else "NO_TRADE"
        reason_parts = [
            "shadow_validation_all_intersection_candidates",
            f"policy_gate={gate}",
            *prediction.gate_reasons,
            "not_a_broker_order",
        ]
        candidate.action_reason = ";".join(dict.fromkeys(reason_parts))
        result.append(candidate)
    return result


__all__ = [
    "build_features",
    "estimate_round_trip_rate",
    "score_candidates",
]
