from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass
from typing import Any, Mapping

from .domain import Candidate, normalize_date
from .feature_contract import production_feature_coverage
from .sources import SOURCE_A, SOURCE_DECISION, SOURCE_PREMIUM


FEATURE_SCHEMA_VERSION = "formal_features_v2"

# Source rows are deliberately *not* flattened into the model input.  Several
# upstream artifacts contain realised outcomes next to point-in-time
# predictors, so an allow-list is the only safe default.
SOURCE_FEATURE_ALLOWLIST: dict[str, frozenset[str]] = {
    SOURCE_A: frozenset(
        {
            "prob_final",
            "p_limit_up_calibrated",
            "final_score",
            "rank_score",
            "auction_strength_score",
            "intraday_available",
            "intraday_confidence_score",
            "intraday_quality_score",
            "intraday_risk_score",
            "intraday_soft_risk_score",
            "intraday_hard_risk_flag",
            "limit_times",
            "open_board_count",
            "late_withdraw_score",
            "reseal_score",
            "stage_prior",
            "stage_quality_weight",
            "stage_risk_penalty",
            "regime_score",
            "regime_smooth",
            "StrengthScore",
            "ThemeBoost",
        }
    ),
    SOURCE_PREMIUM: frozenset(
        {
            "premium_rank_score",
            "premium_final_score",
            "t_up_attack_score",
            "t1_accept_score",
            "t_limitup_prob_calibrated",
            "t1_accept_prob_blend",
            "t1_fail_prob_blend",
            "t1_big_drawdown_prob_blend",
            "t_close_ret_pred",
            "t_intraday_ret_pred",
            "volatility_5d",
            "volatility_10d",
            "volatility_20d",
            "max_drawdown_20d",
            "turnover_rate",
            "turnover_rate_f",
            "volume_ratio",
            "auction_amount",
            "factor_auction_available",
            "factor_auction_strength",
            "factor_intraday_quality",
            "factor_intraday_risk_penalty",
            "tail_risk_score",
            "risk_penalty_score",
            "score_ev",
            "r_p05",
            "r_p25",
            "r_p50",
            "r_p75",
            "r_p95",
            "mkt_avg_ret",
            "mkt_median_ret",
            "mkt_up_ratio",
            "mkt_strong_ratio",
            "mkt_touch_strong_ratio",
            "mkt_amount_sum",
            "board_limit_up_count",
            "board_crowding_rank",
            "is_st_like",
        }
    ),
    SOURCE_DECISION: frozenset(
        {
            "decision_p_fill",
            "decision_e_ret",
            "decision_ev",
            "decision_cost",
            "decision_risk_penalty",
            "predicted_fill_probability",
            "predicted_net_return",
            "predicted_return_lcb",
            "predicted_return_ucb",
            "predicted_profit_probability",
            "predicted_big_loss_probability",
            "predicted_exit_probability",
            "predicted_continuation_limit_up_probability",
            "path_data_coverage",
            "path_strength_delta",
            "path_strength_latest",
            "stage_recent_promotion_rate",
            "stage_recent_promotion_samples",
            "same_industry_stage_count",
            "mechanism_limit_pct",
        }
    ),
}

FORBIDDEN_SOURCE_FIELD_PATTERNS = (
    re.compile(r"^actual_", re.IGNORECASE),
    re.compile(r"^truth_", re.IGNORECASE),
    re.compile(r"^label(?:_|$)", re.IGNORECASE),
    re.compile(r"^realized_", re.IGNORECASE),
    re.compile(r"^observation_t_return$", re.IGNORECASE),
    re.compile(r"^observation_fill(?:_|$)", re.IGNORECASE),
    re.compile(r"^continuation_limit_up_hit$", re.IGNORECASE),
)


@dataclass(frozen=True)
class FeatureSnapshot:
    schema_version: str
    asof_date: str | None
    values: dict[str, Any]
    coverage: float
    market_data_valid: bool
    invalid_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.values,
            "feature_schema_version": self.schema_version,
            "feature_asof_date": self.asof_date,
            "feature_coverage": self.coverage,
            "market_data_valid": self.market_data_valid,
            "market_data_invalid_reasons": list(self.invalid_reasons),
        }


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _number(value: Any) -> float | None:
    if value in (None, "", "null", "None") or isinstance(value, bool):
        return float(value) if isinstance(value, bool) else None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _forbidden_source_field(field: str) -> bool:
    return any(pattern.search(field) is not None for pattern in FORBIDDEN_SOURCE_FIELD_PATTERNS)


def extract_whitelisted_source_features(candidate: Candidate) -> dict[str, float]:
    """Extract only explicitly approved, D-as-of numeric upstream fields."""

    values: dict[str, float] = {}
    for source_id, allowed in SOURCE_FEATURE_ALLOWLIST.items():
        source_values = candidate.source_values.get(source_id, {})
        if not isinstance(source_values, Mapping):
            continue
        for field in sorted(allowed):
            if _forbidden_source_field(field):
                raise ValueError(f"forbidden source feature entered allow-list: {field}")
            parsed = _number(source_values.get(field))
            if parsed is not None:
                values[f"src_{source_id}__{field}"] = parsed

    # Decision currently publishes the probability of a normal T+1 exit.  The
    # formal delay head is expressed in the opposite direction, so expose an
    # explicit derived feature instead of teaching every consumer to invert it
    # (or, worse, silently treating exit probability as delay probability).
    exit_probability_key = f"src_{SOURCE_DECISION}__predicted_exit_probability"
    exit_probability = values.get(exit_probability_key)
    if exit_probability is not None:
        values[
            f"src_{SOURCE_DECISION}__predicted_exit_delay_probability"
        ] = 1.0 - _clip(exit_probability, 0.0, 1.0)
    return values


def _returns(closes: list[float]) -> list[float]:
    return [
        closes[index] / closes[index - 1] - 1.0
        for index in range(1, len(closes))
        if closes[index - 1] > 0
    ]


def _trailing_return(closes: list[float], days: int) -> float | None:
    if len(closes) <= days or closes[-days - 1] <= 0:
        return None
    return closes[-1] / closes[-days - 1] - 1.0


def _window_volatility(returns: list[float], days: int) -> float | None:
    if len(returns) < days:
        return None
    return statistics.pstdev(returns[-days:])


def _downside_volatility(returns: list[float], days: int) -> float | None:
    if len(returns) < days:
        return None
    downside = [min(value, 0.0) for value in returns[-days:]]
    return math.sqrt(statistics.fmean(value * value for value in downside))


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


def _atr_ratio(bars: list[Any], days: int = 14) -> float | None:
    if len(bars) <= days:
        return None
    true_ranges: list[float] = []
    for index in range(len(bars) - days, len(bars)):
        current = bars[index]
        previous_close = float(bars[index - 1].close)
        high = float(current.high)
        low = float(current.low)
        if previous_close <= 0 or high <= 0 or low <= 0 or high < low:
            return None
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    latest_close = float(bars[-1].close)
    return statistics.fmean(true_ranges) / latest_close if latest_close > 0 else None


def _average_amplitude(bars: list[Any], days: int = 20) -> float | None:
    if len(bars) <= days:
        return None
    values: list[float] = []
    for index in range(len(bars) - days, len(bars)):
        previous_close = float(bars[index - 1].close)
        high = float(bars[index].high)
        low = float(bars[index].low)
        if previous_close <= 0 or high <= 0 or low <= 0 or high < low:
            return None
        values.append((high - low) / previous_close)
    return statistics.fmean(values)


def _rank_features(candidate: Candidate, table_sizes: dict[str, int]) -> dict[str, float]:
    percentiles: list[float] = []
    borda_numer = 0.0
    borda_denom = 0.0
    values: dict[str, float] = {}
    for source_id in (SOURCE_A, SOURCE_PREMIUM, SOURCE_DECISION):
        rank = int(candidate.source_ranks.get(source_id, 0) or 0)
        size = max(1, int(table_sizes.get(source_id, rank or 1)))
        if rank <= 0 or rank > size:
            percentile = 0.0
            points = 0.0
        else:
            percentile = 1.0 if size == 1 else 1.0 - (rank - 1) / (size - 1)
            points = size - rank + 1
        values[f"rank_percentile_{source_id}"] = percentile
        percentiles.append(percentile)
        borda_numer += points
        borda_denom += size
    consensus = statistics.fmean(percentiles) if percentiles else 0.0
    disagreement = statistics.pstdev(percentiles) if len(percentiles) > 1 else 0.0
    values.update(
        {
            "rank_consensus": consensus,
            "rank_disagreement": _clip(disagreement * 2.0, 0.0, 1.0),
            "rank_borda": borda_numer / borda_denom if borda_denom else 0.0,
        }
    )
    return values


def _stage_features(candidate: Candidate) -> dict[str, Any]:
    source_values = candidate.source_values.get(SOURCE_DECISION, {})
    stage = str(source_values.get("stage_transition") or "").strip()
    if not re.fullmatch(r"[1-9][0-9]*→[1-9][0-9]*", stage):
        for source_id, field in ((SOURCE_PREMIUM, "晋阶"), (SOURCE_A, "advance_stage")):
            value = str(candidate.source_values.get(source_id, {}).get(field) or "").strip()
            if re.fullmatch(r"[1-9][0-9]*→[1-9][0-9]*", value):
                stage = value
                break
    if not stage:
        return {
            "stage_transition": None,
            "stage_from": None,
            "stage_to": None,
            "stage_is_2_to_3": None,
            "stage_is_3_to_4": None,
        }
    left, right = stage.split("→", 1)
    stage_from = int(left)
    stage_to = int(right)
    if stage_to != stage_from + 1:
        return {
            "stage_transition": None,
            "stage_from": None,
            "stage_to": None,
            "stage_is_2_to_3": None,
            "stage_is_3_to_4": None,
        }
    return {
        "stage_transition": stage,
        "stage_from": float(stage_from),
        "stage_to": float(stage_to),
        "stage_is_2_to_3": 1.0 if stage == "2→3" else 0.0,
        "stage_is_3_to_4": 1.0 if stage == "3→4" else 0.0,
    }


def _source_strength(source_features: dict[str, float]) -> float:
    components: list[float] = []
    a_probability = source_features.get(f"src_{SOURCE_A}__prob_final")
    premium_score = source_features.get(f"src_{SOURCE_PREMIUM}__premium_rank_score")
    decision_ev = source_features.get(f"src_{SOURCE_DECISION}__decision_ev")
    if a_probability is not None:
        components.append(_clip(a_probability, 0.0, 1.0))
    if premium_score is not None:
        components.append(_clip(premium_score / 100.0 if premium_score > 1 else premium_score, 0.0, 1.0))
    if decision_ev is not None:
        components.append(1.0 / (1.0 + math.exp(-_clip(decision_ev * 25.0, -30.0, 30.0))))
    return statistics.fmean(components) if components else 0.5


def build_feature_snapshot(
    candidate: Candidate,
    bars: list[Any],
    table_sizes: dict[str, int],
    *,
    decision_date: str | None = None,
    min_daily_bars: int = 21,
) -> FeatureSnapshot:
    """Build a frozen, point-in-time-safe candidate feature snapshot.

    Invalid or stale market histories are never silently used.  Rank and
    allow-listed source features remain available for deterministic Borda
    fallback, while every market-derived value stays null and the policy gate
    is forced closed by the ranking engine.
    """

    min_bars = max(2, int(min_daily_bars))
    expected_date = normalize_date(decision_date, "feature decision_date") if decision_date else None
    invalid_reasons: list[str] = []
    normalized: list[tuple[str, Any]] = []
    seen_dates: set[str] = set()
    for bar in bars:
        try:
            day = normalize_date(getattr(bar, "date", None), "feature bar date")
        except Exception:
            invalid_reasons.append("invalid_bar_date")
            continue
        if day in seen_dates:
            invalid_reasons.append("duplicate_bar_date")
            continue
        seen_dates.add(day)
        normalized.append((day, bar))
    if normalized != sorted(normalized, key=lambda item: item[0]):
        invalid_reasons.append("bars_not_chronological")
    normalized.sort(key=lambda item: item[0])
    if expected_date and any(day > expected_date for day, _ in normalized):
        invalid_reasons.append("future_bar_present")
    eligible = [(day, bar) for day, bar in normalized if expected_date is None or day <= expected_date]
    if len(eligible) < min_bars:
        invalid_reasons.append("min_daily_bars_not_met")
    if expected_date and (not eligible or eligible[-1][0] != expected_date):
        invalid_reasons.append("last_bar_not_decision_date")

    market_valid = not invalid_reasons
    valid_bars = [bar for _, bar in eligible] if market_valid else []
    if valid_bars:
        for bar in valid_bars:
            try:
                open_price = float(bar.open)
                close_price = float(bar.close)
                high = float(bar.high)
                low = float(bar.low)
                volume = float(bar.volume)
                amount = float(bar.amount)
            except (TypeError, ValueError, AttributeError):
                invalid_reasons.append("invalid_daily_ohlcv")
                break
            if (
                not all(
                    math.isfinite(value) and value > 0
                    for value in (open_price, close_price, high, low)
                )
                or not math.isfinite(volume)
                or not math.isfinite(amount)
                or volume < 0
                or amount < 0
                or high < low
                or low > min(open_price, close_price)
                or high < max(open_price, close_price)
            ):
                invalid_reasons.append("invalid_daily_ohlcv")
                break
        if "invalid_daily_ohlcv" in invalid_reasons:
            market_valid = False
            valid_bars = []
    closes: list[float] = []
    if valid_bars:
        try:
            closes = [float(bar.close) for bar in valid_bars]
        except (TypeError, ValueError):
            closes = []
        if not closes or any(not math.isfinite(value) or value <= 0 for value in closes):
            invalid_reasons.append("invalid_close_price")
            market_valid = False
            valid_bars = []
            closes = []

    returns = _returns(closes)
    amounts = [
        float(bar.amount)
        for bar in valid_bars[-20:]
        if _number(getattr(bar, "amount", None)) is not None and float(bar.amount) > 0
    ]
    volumes = [
        float(bar.volume)
        for bar in valid_bars[-20:]
        if _number(getattr(bar, "volume", None)) is not None and float(bar.volume) > 0
    ]
    turnovers = [
        float(bar.turnover)
        for bar in valid_bars[-20:]
        if _number(getattr(bar, "turnover", None)) is not None
    ]

    market_values: dict[str, Any] = {
        "ret_1d": _trailing_return(closes, 1),
        "ret_3d": _trailing_return(closes, 3),
        "ret_5d": _trailing_return(closes, 5),
        "ret_10d": _trailing_return(closes, 10),
        "ret_20d": _trailing_return(closes, 20),
        "volatility_5d": _window_volatility(returns, 5),
        "volatility_20d": _window_volatility(returns, 20),
        "downside_volatility_20d": _downside_volatility(returns, 20),
        "atr_14d": _atr_ratio(valid_bars, 14),
        "amplitude_20d": _average_amplitude(valid_bars, 20),
        "cvar_loss_10pct": _cvar_loss(returns[-60:]),
        "max_drawdown_20d": _max_drawdown(closes[-21:]),
        "avg_amount_20d": statistics.fmean(amounts) if amounts else None,
        "avg_volume_20d": statistics.fmean(volumes) if volumes else None,
        "avg_turnover_20d": statistics.fmean(turnovers) if turnovers else None,
        "bar_count": float(len(eligible)),
        "market_data_providers": sorted(
            {str(getattr(bar, "provider", "UNSPECIFIED")).upper() for _, bar in eligible}
        ),
        "daily_price_adjustments": sorted(
            {str(getattr(bar, "price_adjustment", "UNSPECIFIED")).upper() for _, bar in eligible}
        ),
    }
    rank_values = _rank_features(candidate, table_sizes)
    stage_values = _stage_features(candidate)
    source_values = extract_whitelisted_source_features(candidate)
    values = {**market_values, **rank_values, **stage_values, **source_values}
    values["source_strength"] = _source_strength(source_values)

    coverage = production_feature_coverage(values)
    if not market_valid:
        coverage = 0.0
    # Preserve deterministic order while avoiding duplicate reason strings.
    reasons = tuple(dict.fromkeys(invalid_reasons))
    return FeatureSnapshot(
        schema_version=FEATURE_SCHEMA_VERSION,
        asof_date=expected_date or (eligible[-1][0] if eligible else None),
        values=values,
        coverage=coverage,
        market_data_valid=market_valid,
        invalid_reasons=reasons,
    )


__all__ = [
    "FEATURE_SCHEMA_VERSION",
    "FORBIDDEN_SOURCE_FIELD_PATTERNS",
    "SOURCE_FEATURE_ALLOWLIST",
    "FeatureSnapshot",
    "build_feature_snapshot",
    "extract_whitelisted_source_features",
]
