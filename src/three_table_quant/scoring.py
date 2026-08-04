from __future__ import annotations

import math
import statistics
from typing import Any

from .domain import Candidate
from .sources import SOURCE_A, SOURCE_DECISION, SOURCE_PREMIUM, numeric_source_value


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-_clip(value, -30.0, 30.0)))


def _returns(closes: list[float]) -> list[float]:
    return [closes[index] / closes[index - 1] - 1.0 for index in range(1, len(closes)) if closes[index - 1] > 0]


def _trailing_return(closes: list[float], days: int) -> float | None:
    if len(closes) <= days or closes[-days - 1] <= 0:
        return None
    return closes[-1] / closes[-days - 1] - 1.0


def _max_drawdown(closes: list[float]) -> float | None:
    if not closes:
        return None
    peak = closes[0]
    worst = 0.0
    for value in closes:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst


def _cvar_loss(returns: list[float], fraction: float = 0.10) -> float | None:
    if not returns:
        return None
    count = max(1, math.ceil(len(returns) * fraction))
    tail = sorted(returns)[:count]
    return max(0.0, -statistics.fmean(tail))


def _source_strength(candidate: Candidate) -> float:
    a_probability = numeric_source_value(candidate, SOURCE_A, "prob_final", "Probability", "prob")
    premium_score = numeric_source_value(
        candidate,
        SOURCE_PREMIUM,
        "premium_rank_score",
        "premium_final_score",
        "t_up_attack_score",
    )
    decision_ev = numeric_source_value(
        candidate,
        SOURCE_DECISION,
        "decision_ev",
        "predicted_net_return",
        "decision_e_ret",
    )
    components: list[float] = []
    if a_probability is not None:
        components.append(_clip(a_probability, 0.0, 1.0))
    if premium_score is not None:
        components.append(_clip(premium_score / 100.0 if premium_score > 1 else premium_score, 0.0, 1.0))
    if decision_ev is not None:
        components.append(_sigmoid(decision_ev * 25.0))
    return statistics.fmean(components) if components else 0.5


def _rank_consensus(candidate: Candidate, table_sizes: dict[str, int]) -> tuple[float, float]:
    percentiles: list[float] = []
    for source_id, rank in candidate.source_ranks.items():
        size = max(1, table_sizes.get(source_id, rank))
        percentiles.append(1.0 if size == 1 else 1.0 - (rank - 1) / (size - 1))
    if not percentiles:
        return 0.0, 1.0
    disagreement = statistics.pstdev(percentiles) if len(percentiles) > 1 else 0.0
    return statistics.fmean(percentiles), _clip(disagreement * 2.0, 0.0, 1.0)


def build_features(candidate: Candidate, bars: list[Any], table_sizes: dict[str, int]) -> dict[str, Any]:
    closes = [float(bar.close) for bar in bars if float(bar.close) > 0]
    returns = _returns(closes)
    trailing = closes[-21:] if closes else []
    amounts = [float(bar.amount) for bar in bars[-20:] if float(bar.amount) > 0]
    volumes = [float(bar.volume) for bar in bars[-20:] if float(bar.volume) > 0]
    turnovers = [float(bar.turnover) for bar in bars[-20:] if getattr(bar, "turnover", None) is not None]
    providers = sorted({str(getattr(bar, "provider", "UNSPECIFIED")).upper() for bar in bars})
    adjustments = sorted(
        {str(getattr(bar, "price_adjustment", "UNSPECIFIED")).upper() for bar in bars}
    )
    consensus, disagreement = _rank_consensus(candidate, table_sizes)
    return {
        "ret_5d": _trailing_return(closes, 5),
        "ret_20d": _trailing_return(closes, 20),
        "volatility_20d": statistics.pstdev(returns[-20:]) if len(returns) >= 2 else None,
        "cvar_loss_10pct": _cvar_loss(returns[-60:]),
        "max_drawdown_20d": _max_drawdown(trailing),
        "avg_amount_20d": statistics.fmean(amounts) if amounts else None,
        # Some public daily feeds expose raw volume but not daily amount.  A
        # cross-sectional volume proxy is still useful for ranking liquidity;
        # execution itself never uses this proxy and continues to require
        # self-consistent minute amount and volume.
        "avg_volume_20d": statistics.fmean(volumes) if volumes else None,
        "avg_turnover_20d": statistics.fmean(turnovers) if turnovers else None,
        "rank_consensus": consensus,
        "rank_disagreement": disagreement,
        "source_strength": _source_strength(candidate),
        "bar_count": float(len(bars)),
        "market_data_providers": providers,
        "daily_price_adjustments": adjustments,
    }


def _zscore(values: list[float | None]) -> list[float]:
    valid = [value for value in values if value is not None and math.isfinite(value)]
    if len(valid) < 2:
        return [0.0 for _ in values]
    center = statistics.median(valid)
    deviations = [abs(value - center) for value in valid]
    mad = statistics.median(deviations)
    scale = 1.4826 * mad
    if scale < 1e-12:
        scale = statistics.pstdev(valid)
    if scale < 1e-12:
        return [0.0 for _ in values]
    return [0.0 if value is None else _clip((value - center) / scale, -3.0, 3.0) for value in values]


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
) -> list[Candidate]:
    if not candidates:
        return []
    for candidate in candidates:
        candidate.features = build_features(candidate, bars_by_code.get(candidate.ts_code, []), table_sizes)

    feature_names = [
        "ret_5d",
        "ret_20d",
        "volatility_20d",
        "cvar_loss_10pct",
        "max_drawdown_20d",
        "avg_amount_20d",
        "rank_consensus",
        "source_strength",
    ]
    zscores = {
        name: _zscore([candidate.features.get(name) for candidate in candidates]) for name in feature_names
    }
    ranking = config["ranking"]
    execution = config["execution"]
    cost_rate = estimate_round_trip_rate(execution)
    for index, candidate in enumerate(candidates):
        features = candidate.features
        missing_flags = [
            features.get("ret_5d") is None,
            features.get("ret_20d") is None,
            features.get("volatility_20d") is None,
            features.get("cvar_loss_10pct") is None,
            features.get("avg_amount_20d") is None and features.get("avg_volume_20d") is None,
        ]
        missing_fraction = sum(missing_flags) / len(missing_flags)
        momentum_z = 0.45 * zscores["ret_5d"][index] + 0.55 * zscores["ret_20d"][index]
        volatility_z = zscores["volatility_20d"][index]
        liquidity_value = features.get("avg_amount_20d") or features.get("avg_volume_20d")
        liquidity_log = math.log10(max(float(liquidity_value or 1.0), 1.0))
        liquidity_logs = [
            math.log10(
                max(
                    float(
                        item.features.get("avg_amount_20d")
                        or item.features.get("avg_volume_20d")
                        or 1.0
                    ),
                    1.0,
                )
            )
            for item in candidates
        ]
        liquidity_z = _zscore(liquidity_logs)[index]
        consensus_z = zscores["rank_consensus"][index]
        strength_z = zscores["source_strength"][index]
        p_fill = _clip(_sigmoid(-0.35 + 0.32 * liquidity_z - 0.22 * volatility_z + 0.15 * consensus_z), 0.05, 0.95)
        expected_gross = 0.0028 * momentum_z + 0.0012 * consensus_z + 0.0008 * strength_z
        expected_net = expected_gross - cost_rate
        cvar_loss = float(features.get("cvar_loss_10pct") or 0.04)
        drawdown = abs(float(features.get("max_drawdown_20d") or -0.10))
        p_exit_delay = _clip(_sigmoid(-2.2 - 0.45 * liquidity_z + 0.35 * volatility_z + 2.0 * drawdown), 0.01, 0.90)
        uncertainty = _clip(
            0.55 * missing_fraction + 0.45 * float(features.get("rank_disagreement") or 0.0),
            0.0,
            1.0,
        )
        utility = p_fill * (
            expected_net
            - float(ranking["cvar_weight"]) * cvar_loss
            - float(ranking["exit_delay_weight"]) * p_exit_delay
            - float(ranking["uncertainty_weight"]) * uncertainty
        )
        candidate.metrics = {
            "p_fill_0925": p_fill,
            "expected_gross_return": expected_gross,
            "expected_net_return": expected_net,
            "cvar_loss_10pct": cvar_loss,
            "p_exit_delay": p_exit_delay,
            "uncertainty": uncertainty,
            "estimated_round_trip_rate": cost_rate,
            "utility_score": utility,
            "missing_fraction": missing_fraction,
        }

    candidates.sort(
        key=lambda item: (
            -float(item.metrics.get("utility_score") or -999.0),
            -float(item.features.get("rank_consensus") or 0.0),
            item.ts_code,
        )
    )
    for rank, candidate in enumerate(candidates, start=1):
        candidate.rank = rank
        metrics = candidate.metrics
        reasons: list[str] = []
        if float(metrics["p_fill_0925"]) < float(ranking["min_fill_probability"]):
            reasons.append("p_fill_below_threshold")
        if float(metrics["expected_net_return"]) < float(ranking["min_expected_net_return"]):
            reasons.append("expected_net_return_not_positive")
        if float(metrics["utility_score"]) < float(ranking["min_utility_score"]):
            reasons.append("risk_adjusted_utility_not_positive")
        if float(metrics["missing_fraction"]) > float(ranking["max_missing_fraction"]):
            reasons.append("market_features_incomplete")
        metrics["policy_trade_eligible"] = not reasons
        # Every member of the strict three-table intersection is observed.
        # Policy eligibility remains an independent diagnostic and never
        # creates a broker order in this shadow-only system.
        candidate.action = "SHADOW"
        if reasons:
            candidate.action_reason = (
                "shadow_validation_all_intersection_candidates;"
                "policy_gate=NO_TRADE;"
                + ";".join(reasons)
                + ";not_a_broker_order"
            )
        else:
            candidate.action_reason = (
                "shadow_validation_all_intersection_candidates;"
                "policy_gate=TRADE;not_a_broker_order"
            )
    return candidates
