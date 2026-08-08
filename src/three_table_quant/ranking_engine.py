from __future__ import annotations

import json
import math
import statistics
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .domain import Candidate, ContractError, normalize_date
from .feature_contract import model_eligible_feature_names
from .features import FEATURE_SCHEMA_VERSION
from .promotion import validate_certified_artifact
from .sources import SOURCE_A, SOURCE_DECISION, SOURCE_PREMIUM


MODEL_ID = "transparent_shadow_champion_v2"
PREDICTION_SCHEMA_VERSION = "ranking_prediction_v2"
LEARNED_ARTIFACT_SCHEMA_VERSION = "model_artifact_v1"


class ArtifactValidationError(ValueError):
    """Raised when a learned ranking artifact is unsafe or incompatible."""


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _sigmoid(value: float) -> float:
    bounded = _clip(value, -30.0, 30.0)
    return 1.0 / (1.0 + math.exp(-bounded))


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _value(features: dict[str, Any], key: str, default: float) -> float:
    parsed = _finite(features.get(key))
    return default if parsed is None else parsed


def _source_key(source_id: str, field: str) -> str:
    return f"src_{source_id}__{field}"


def _available(features: dict[str, Any], *keys: str) -> list[float]:
    values: list[float] = []
    for key in keys:
        value = _finite(features.get(key))
        if value is not None:
            values.append(value)
    return values


@dataclass(frozen=True)
class PredictionBundle:
    schema_version: str
    model_id: str
    model_stage: str
    feature_schema_version: str
    prediction_status: str
    p_fill: float
    expected_fill_ratio: float
    conditional_net_return_mean: float
    conditional_net_return_q10: float
    conditional_net_return_q50: float
    conditional_net_return_q90: float
    p_exit_delay: float
    expected_delay_days: float
    p_promotion: float
    uncertainty: float
    expected_shortfall: float
    utility: float
    gate_decision: str
    gate_reasons: tuple[str, ...]
    ranking_fallback: bool

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["gate_reasons"] = list(self.gate_reasons)
        return payload


def _finalize_prediction(
    *,
    ranking: dict[str, Any],
    features: dict[str, Any],
    missing_fraction: float,
    model_id: str,
    model_stage: str,
    prediction_status: str,
    p_fill: float,
    expected_fill_ratio: float,
    conditional_mean: float,
    q10: float,
    q50: float,
    q90: float,
    p_exit_delay: float,
    expected_delay_days: float,
    p_promotion: float,
    uncertainty: float,
    expected_shortfall: float,
    force_borda_fallback: bool,
) -> PredictionBundle:
    numeric = (
        p_fill,
        expected_fill_ratio,
        conditional_mean,
        q10,
        q50,
        q90,
        p_exit_delay,
        expected_delay_days,
        p_promotion,
        uncertainty,
        expected_shortfall,
    )
    if not all(math.isfinite(float(value)) for value in numeric):
        raise ArtifactValidationError("prediction heads must all be finite")
    # Independent quantile regressions can cross.  Apply a deterministic
    # monotone projection before risk, utility or gating consumes the interval.
    ordered = sorted((float(q10), float(q50), float(q90)))
    q10, q50, q90 = ordered
    p_fill = _clip(float(p_fill), 0.0, 1.0)
    expected_fill_ratio = _clip(float(expected_fill_ratio), 0.0, 1.0)
    assume_open_fill = bool(ranking.get("assume_open_fill", False))
    if assume_open_fill:
        p_fill = 1.0
        expected_fill_ratio = 1.0
    p_exit_delay = _clip(float(p_exit_delay), 0.0, 1.0)
    p_promotion = _clip(float(p_promotion), 0.0, 1.0)
    uncertainty = _clip(float(uncertainty), 0.0, 1.0)
    expected_shortfall = max(0.0, float(expected_shortfall))
    if expected_delay_days <= 0:
        raise ArtifactValidationError("expected_delay_days must be positive")

    cvar_weight = float(ranking.get("cvar_weight", 0.25))
    delay_weight = float(ranking.get("exit_delay_weight", 0.005))
    uncertainty_weight = float(ranking.get("uncertainty_weight", 0.003))
    utility = (
        p_fill * conditional_mean
        - cvar_weight * p_fill * expected_shortfall
        - delay_weight * p_fill * p_exit_delay * expected_delay_days
        - uncertainty_weight * uncertainty
    )
    if not math.isfinite(utility):
        raise ArtifactValidationError("risk utility must be finite")

    market_valid = features.get("market_data_valid") is True
    reasons: list[str] = []
    if not market_valid:
        reasons.append("market_features_incomplete")
    if missing_fraction > float(ranking.get("max_missing_fraction", 0.34)):
        reasons.append("market_features_incomplete")
    if (
        not assume_open_fill
        and p_fill < float(ranking.get("min_fill_probability", 0.40))
    ):
        reasons.append("p_fill_below_threshold")
    if conditional_mean <= float(ranking.get("min_expected_net_return", 0.0)):
        reasons.append("expected_net_return_not_positive")
    if q10 <= float(ranking.get("min_return_lcb", 0.0)):
        reasons.append("conditional_return_lcb_not_positive")
    if p_exit_delay > float(ranking.get("max_exit_delay_probability", 0.50)):
        reasons.append("exit_delay_risk_above_threshold")
    if utility <= float(ranking.get("min_utility_score", 0.0)):
        reasons.append("risk_adjusted_utility_not_positive")
    if force_borda_fallback:
        reasons.append("cohort_ranking_fallback_borda")
    reasons = list(dict.fromkeys(reasons))
    fallback = force_borda_fallback or not market_valid
    return PredictionBundle(
        schema_version=PREDICTION_SCHEMA_VERSION,
        model_id=model_id,
        model_stage=model_stage,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        prediction_status="BORDA_FALLBACK" if fallback else prediction_status,
        p_fill=p_fill,
        expected_fill_ratio=expected_fill_ratio,
        conditional_net_return_mean=float(conditional_mean),
        conditional_net_return_q10=q10,
        conditional_net_return_q50=q50,
        conditional_net_return_q90=q90,
        p_exit_delay=p_exit_delay,
        expected_delay_days=float(expected_delay_days),
        p_promotion=p_promotion,
        uncertainty=uncertainty,
        expected_shortfall=expected_shortfall,
        utility=utility,
        gate_decision="NO_TRADE" if reasons else "TRADE",
        gate_reasons=tuple(reasons),
        ranking_fallback=fallback,
    )


class TransparentChampionV2:
    """Deterministic champion used until a validated learned model exists.

    The four output heads mirror the future learned engine, but the current
    coefficients are intentionally fixed and auditable.  This is a baseline,
    not a claim that any probability has already been statistically promoted.
    """

    model_id = MODEL_ID
    feature_schema_version = FEATURE_SCHEMA_VERSION

    def __init__(
        self,
        ranking_config: dict[str, Any],
        *,
        estimated_round_trip_rate: float,
    ) -> None:
        self.ranking = ranking_config
        self.cost_rate = max(0.0, float(estimated_round_trip_rate))

    @staticmethod
    def _liquidity_signal(features: dict[str, Any]) -> float:
        """Use fixed unit-aware transforms; never mix amount and volume values."""

        amount = _finite(features.get("avg_amount_20d"))
        if amount is not None and amount > 0:
            return _clip((math.log10(amount) - 8.0) / 2.0, -1.5, 1.5)
        volume = _finite(features.get("avg_volume_20d"))
        if volume is not None and volume > 0:
            # Public A-share daily feeds used here declare volume in lots.  It
            # is a separate fallback transform, never a value in an amount
            # cross-section.
            return _clip((math.log10(volume) - 5.5) / 2.0, -1.5, 1.5)
        return -1.5

    @staticmethod
    def _source_fill_prior(features: dict[str, Any]) -> tuple[float, float]:
        raw = _available(
            features,
            _source_key(SOURCE_DECISION, "decision_p_fill"),
            _source_key(SOURCE_DECISION, "predicted_fill_probability"),
            _source_key(SOURCE_PREMIUM, "factor_auction_available"),
        )
        values = [_clip(value, 0.0, 1.0) for value in raw]
        if not values:
            return 0.5, 1.0
        return statistics.fmean(values), statistics.pstdev(values) if len(values) > 1 else 0.25

    @staticmethod
    def _source_return_prior(features: dict[str, Any]) -> tuple[float, float]:
        raw = _available(
            features,
            _source_key(SOURCE_DECISION, "decision_e_ret"),
            _source_key(SOURCE_DECISION, "predicted_net_return"),
            _source_key(SOURCE_PREMIUM, "t_close_ret_pred"),
            _source_key(SOURCE_PREMIUM, "r_p50"),
        )
        values = [_clip(value, -0.08, 0.12) for value in raw]
        if not values:
            return 0.0, 0.04
        return statistics.fmean(values), statistics.pstdev(values) if len(values) > 1 else 0.02

    @staticmethod
    def _promotion_probability(features: dict[str, Any]) -> float:
        priors = _available(
            features,
            _source_key(SOURCE_A, "p_limit_up_calibrated"),
            _source_key(SOURCE_A, "prob_final"),
            _source_key(SOURCE_PREMIUM, "t_limitup_prob_calibrated"),
            _source_key(SOURCE_DECISION, "predicted_continuation_limit_up_probability"),
            _source_key(SOURCE_DECISION, "stage_recent_promotion_rate"),
        )
        if priors:
            probability = statistics.fmean(_clip(value, 0.0, 1.0) for value in priors)
        else:
            rank_consensus = _value(features, "rank_consensus", 0.0)
            ret_5d = _value(features, "ret_5d", 0.0)
            probability = _sigmoid(-1.35 + 0.9 * rank_consensus + 1.2 * _clip(ret_5d, -0.3, 0.3))
        stage = str(features.get("stage_transition") or "")
        if stage == "3→4":
            probability *= 0.92
        return _clip(probability, 0.01, 0.99)

    def predict(
        self,
        features: dict[str, Any],
        *,
        missing_fraction: float,
        force_borda_fallback: bool = False,
        decision_date: str | None = None,
    ) -> PredictionBundle:
        del decision_date
        coverage = _clip(_value(features, "feature_coverage", 0.0), 0.0, 1.0)
        rank_consensus = _clip(_value(features, "rank_consensus", 0.0), 0.0, 1.0)
        rank_disagreement = _clip(_value(features, "rank_disagreement", 1.0), 0.0, 1.0)
        liquidity = self._liquidity_signal(features)
        volatility = max(0.0, _value(features, "volatility_20d", 0.08))
        volatility_5d = max(0.0, _value(features, "volatility_5d", volatility))
        downside_volatility = max(
            0.0,
            _value(features, "downside_volatility_20d", volatility / math.sqrt(2.0)),
        )
        drawdown_raw = _finite(features.get("max_drawdown_20d"))
        drawdown = abs(drawdown_raw) if drawdown_raw is not None else 0.10
        historical_cvar_raw = _finite(features.get("cvar_loss_10pct"))
        historical_cvar = historical_cvar_raw if historical_cvar_raw is not None else 0.04

        source_fill, fill_dispersion = self._source_fill_prior(features)
        if bool(self.ranking.get("assume_open_fill", False)):
            p_fill = 1.0
            expected_fill_ratio = 1.0
            fill_dispersion = 0.0
        else:
            fill_logit = (
                -0.55
                + 0.48 * liquidity
                + 0.75 * (rank_consensus - 0.5)
                - 3.0 * _clip(volatility, 0.0, 0.20)
                - 1.8 * _clip(downside_volatility, 0.0, 0.20)
                + 0.30 * (source_fill - 0.5)
            )
            p_fill = _clip(_sigmoid(fill_logit), 0.03, 0.97)
            expected_fill_ratio = _clip(
                p_fill * (0.75 + 0.15 * max(liquidity, -1.0)),
                0.01,
                1.0,
            )

        return_prior, return_dispersion = self._source_return_prior(features)
        ret_5d = _clip(_value(features, "ret_5d", 0.0), -0.40, 0.60)
        ret_20d = _clip(_value(features, "ret_20d", 0.0), -0.60, 1.00)
        expected_gross = (
            0.015 * ret_5d
            + 0.006 * ret_20d
            + 0.002 * (rank_consensus - 0.5)
            + 0.10 * return_prior
        )
        conditional_mean = expected_gross - self.cost_rate
        source_tail_widths = _available(
            features,
            _source_key(SOURCE_DECISION, "predicted_return_lcb"),
            _source_key(SOURCE_DECISION, "predicted_return_ucb"),
            _source_key(SOURCE_PREMIUM, "r_p05"),
            _source_key(SOURCE_PREMIUM, "r_p95"),
        )
        source_scale = 0.0
        if len(source_tail_widths) >= 2:
            source_scale = (max(source_tail_widths) - min(source_tail_widths)) / 3.29
        sigma = max(
            0.005,
            volatility_5d,
            volatility,
            downside_volatility * 1.25,
            source_scale,
        )
        q50 = conditional_mean
        q10 = conditional_mean - 1.2815515655446004 * sigma
        q90 = conditional_mean + 1.2815515655446004 * sigma
        expected_shortfall = max(
            0.0,
            historical_cvar,
            1.7549833193248683 * sigma - conditional_mean,
        )

        delay_logit = (
            -2.15
            - 0.50 * liquidity
            + 3.5 * _clip(volatility, 0.0, 0.20)
            + 2.2 * _clip(drawdown, 0.0, 0.60)
        )
        p_exit_delay = _clip(_sigmoid(delay_logit), 0.01, 0.95)
        expected_delay_days = _clip(
            1.0 + 8.0 * _clip(volatility, 0.0, 0.25) + 3.0 * _clip(drawdown, 0.0, 0.60),
            1.0,
            5.0,
        )
        p_promotion = self._promotion_probability(features)
        source_uncertainty = _clip(
            0.5 * fill_dispersion + 4.0 * return_dispersion,
            0.0,
            1.0,
        )
        uncertainty = _clip(
            0.40 * (1.0 - coverage)
            + 0.30 * rank_disagreement
            + 0.15 * source_uncertainty
            + 0.15 * _clip(missing_fraction, 0.0, 1.0),
            0.0,
            1.0,
        )

        return _finalize_prediction(
            ranking=self.ranking,
            features=features,
            missing_fraction=missing_fraction,
            model_id=self.model_id,
            model_stage="CHAMPION_BASELINE",
            prediction_status="SCORED",
            p_fill=p_fill,
            expected_fill_ratio=expected_fill_ratio,
            conditional_mean=conditional_mean,
            q10=q10,
            q50=q50,
            q90=q90,
            p_exit_delay=p_exit_delay,
            expected_delay_days=expected_delay_days,
            p_promotion=p_promotion,
            uncertainty=uncertainty,
            expected_shortfall=expected_shortfall,
            force_borda_fallback=force_borda_fallback,
        )


def _artifact_payload(source: Any) -> dict[str, Any]:
    if isinstance(source, Mapping):
        return dict(source)
    if isinstance(source, bytes):
        try:
            payload = json.loads(source.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactValidationError("learned artifact is invalid JSON") from exc
    elif isinstance(source, (str, Path)):
        raw = str(source)
        try:
            if isinstance(source, str) and raw.lstrip().startswith("{"):
                payload = json.loads(raw)
            else:
                payload = json.loads(Path(source).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactValidationError("learned artifact cannot be loaded") from exc
    else:
        raise ArtifactValidationError("learned artifact must be JSON, a path, or an object")
    if not isinstance(payload, dict):
        raise ArtifactValidationError("learned artifact root must be an object")
    return payload


def _finite_required(value: Any, field: str) -> float:
    parsed = _finite(value)
    if parsed is None:
        raise ArtifactValidationError(f"{field} must be a finite number")
    return parsed


def _normalization_vector(
    normalization: Mapping[str, Any],
    key: str,
    feature_order: tuple[str, ...],
) -> tuple[float, ...]:
    raw = normalization.get(key)
    if raw is None and key == "mean":
        raw = normalization.get("center")
    if isinstance(raw, Mapping):
        if set(raw) != set(feature_order):
            raise ArtifactValidationError(f"normalization.{key} feature set mismatch")
        values = [raw[field] for field in feature_order]
    elif isinstance(raw, list):
        values = raw
    else:
        raise ArtifactValidationError(f"normalization.{key} must be a vector or feature map")
    if len(values) != len(feature_order):
        raise ArtifactValidationError(f"normalization.{key} length mismatch")
    parsed = tuple(
        _finite_required(value, f"normalization.{key}[{index}]")
        for index, value in enumerate(values)
    )
    if key == "scale" and any(value <= 0 for value in parsed):
        raise ArtifactValidationError("normalization.scale values must be positive")
    return parsed


def _head_coefficients(
    head: Mapping[str, Any],
    feature_order: tuple[str, ...],
    head_name: str,
) -> tuple[float, ...]:
    raw = head.get("coefficients")
    if isinstance(raw, Mapping):
        if set(raw) != set(feature_order):
            raise ArtifactValidationError(f"heads.{head_name}.coefficients feature set mismatch")
        values = [raw[field] for field in feature_order]
    elif isinstance(raw, list):
        values = raw
    else:
        raise ArtifactValidationError(f"heads.{head_name}.coefficients must be a vector or feature map")
    if len(values) != len(feature_order):
        raise ArtifactValidationError(f"heads.{head_name}.coefficients length mismatch")
    return tuple(
        _finite_required(value, f"heads.{head_name}.coefficients[{index}]")
        for index, value in enumerate(values)
    )


class LearnedChallenger:
    """Validated JSON-only learned inference with no pickle/code execution."""

    required_heads = (
        "fill",
        "return_mean",
        "return_q10",
        "return_q50",
        "return_q90",
        "delay",
        "delay_days",
        "promotion",
    )
    head_aliases = {
        "fill": ("fill",),
        "return_mean": ("return_mean",),
        "return_q10": ("return_q10", "q10"),
        "return_q50": ("return_q50", "q50"),
        "return_q90": ("return_q90", "q90"),
        "delay": ("delay",),
        "delay_days": ("delay_days",),
        "promotion": ("promotion",),
    }
    head_types = {
        "fill": {"l2_logistic", "logistic"},
        "return_mean": {"huber_linear", "identity"},
        "return_q10": {"pinball_linear", "identity"},
        "return_q50": {"pinball_linear", "identity"},
        "return_q90": {"pinball_linear", "identity"},
        "delay": {"l2_logistic", "logistic"},
        "delay_days": {"positive_linear", "softplus"},
        "promotion": {"l2_logistic", "logistic"},
    }

    def __init__(
        self,
        artifact: Any,
        ranking_config: dict[str, Any],
        *,
        estimated_round_trip_rate: float,
        expected_model_id: str | None = None,
    ) -> None:
        del estimated_round_trip_rate  # learned return heads target net returns
        payload = _artifact_payload(artifact)
        schema = payload.get("schema") or payload.get("schema_version")
        if schema != LEARNED_ARTIFACT_SCHEMA_VERSION:
            raise ArtifactValidationError("unsupported learned artifact schema")
        model_id = str(payload.get("model_id") or "").strip()
        if not model_id:
            raise ArtifactValidationError("learned artifact model_id is required")
        if expected_model_id and model_id != expected_model_id:
            raise ArtifactValidationError("resolved model_id does not match artifact")
        feature_schema = payload.get("feature_schema") or payload.get("feature_schema_version")
        if feature_schema != FEATURE_SCHEMA_VERSION:
            raise ArtifactValidationError("learned artifact feature schema mismatch")
        try:
            trained_through = normalize_date(payload.get("trained_through"), "trained_through")
        except Exception as exc:
            raise ArtifactValidationError("learned artifact trained_through is invalid") from exc

        raw_feature_order = payload.get("feature_order")
        if (
            not isinstance(raw_feature_order, list)
            or not raw_feature_order
            or not all(isinstance(field, str) and field.strip() for field in raw_feature_order)
        ):
            raise ArtifactValidationError("learned artifact feature_order must be a non-empty string list")
        feature_order = tuple(field.strip() for field in raw_feature_order)
        if len(feature_order) != len(set(feature_order)):
            raise ArtifactValidationError("learned artifact feature_order contains duplicates")
        forbidden = (
            "actual_",
            "truth_",
            "realized_",
            "observation_t_return",
            "observation_fill",
            "continuation_limit_up_hit",
        )
        if any(token in field.lower() for field in feature_order for token in forbidden):
            raise ArtifactValidationError("learned artifact contains a future-aware feature")
        model_eligible = frozenset(model_eligible_feature_names())
        if any(field not in model_eligible for field in feature_order):
            raise ArtifactValidationError(
                "learned artifact contains a non-model-eligible feature"
            )

        normalization = payload.get("normalization")
        if not isinstance(normalization, Mapping):
            raise ArtifactValidationError("learned artifact normalization must be an object")
        median = _normalization_vector(normalization, "median", feature_order)
        mean = _normalization_vector(normalization, "mean", feature_order)
        scale = _normalization_vector(normalization, "scale", feature_order)

        raw_heads = payload.get("heads")
        allowed_head_names = {
            alias for aliases in self.head_aliases.values() for alias in aliases
        }
        if not isinstance(raw_heads, Mapping) or not set(raw_heads) <= allowed_head_names:
            raise ArtifactValidationError("learned artifact heads are incomplete or contain extras")
        heads: dict[str, dict[str, Any]] = {}
        for name in self.required_heads:
            matches = [alias for alias in self.head_aliases[name] if alias in raw_heads]
            if len(matches) != 1:
                raise ArtifactValidationError("learned artifact heads are incomplete or ambiguous")
            raw_name = matches[0]
            raw_head = raw_heads[raw_name]
            if not isinstance(raw_head, Mapping):
                raise ArtifactValidationError(f"heads.{name} must be an object")
            head_type = str(raw_head.get("type") or raw_head.get("link") or "").strip()
            if head_type not in self.head_types[name]:
                raise ArtifactValidationError(f"heads.{name}.type is incompatible")
            heads[name] = {
                "type": head_type,
                "intercept": _finite_required(raw_head.get("intercept"), f"heads.{name}.intercept"),
                "coefficients": _head_coefficients(raw_head, feature_order, raw_name),
            }
            if name == "delay_days":
                minimum_output = _finite(raw_head.get("minimum_output"))
                if minimum_output is None:
                    minimum_output = 0.0
                if minimum_output < 0:
                    raise ArtifactValidationError("heads.delay_days.minimum_output must be nonnegative")
                heads[name]["minimum_output"] = minimum_output

        try:
            validate_certified_artifact(payload)
        except ContractError as exc:
            raise ArtifactValidationError(
                f"learned artifact promotion certificate invalid: {exc}"
            ) from exc

        self.ranking = ranking_config
        self.model_id = model_id
        self.assume_open_fill = (
            payload.get("entry_fill_policy") == "T_DAILY_OPEN_FULL_FILL"
        )
        self.feature_schema_version = FEATURE_SCHEMA_VERSION
        self.trained_through = trained_through
        self.feature_order = feature_order
        self.median = median
        self.mean = mean
        self.scale = scale
        self.heads = heads

    def _vector(self, features: dict[str, Any]) -> tuple[float, ...]:
        result: list[float] = []
        for index, field in enumerate(self.feature_order):
            value = _finite(features.get(field))
            imputed = self.median[index] if value is None else value
            normalized = (imputed - self.mean[index]) / self.scale[index]
            if not math.isfinite(normalized):
                raise ArtifactValidationError("normalized feature vector is not finite")
            result.append(normalized)
        return tuple(result)

    def _raw_head(self, name: str, vector: tuple[float, ...]) -> float:
        head = self.heads[name]
        value = float(head["intercept"]) + sum(
            coefficient * feature
            for coefficient, feature in zip(head["coefficients"], vector, strict=True)
        )
        if not math.isfinite(value):
            raise ArtifactValidationError(f"heads.{name} produced a non-finite value")
        return value

    @staticmethod
    def _positive(value: float) -> float:
        if value > 30.0:
            return value
        if value < -30.0:
            return math.exp(value)
        return math.log1p(math.exp(value))

    def predict(
        self,
        features: dict[str, Any],
        *,
        missing_fraction: float,
        force_borda_fallback: bool = False,
        decision_date: str | None = None,
    ) -> PredictionBundle:
        if decision_date is None:
            raise ArtifactValidationError("learned inference requires decision_date")
        try:
            decision = normalize_date(decision_date, "learned inference decision_date")
        except Exception as exc:
            raise ArtifactValidationError("learned inference decision_date is invalid") from exc
        if self.trained_through >= decision:
            raise ArtifactValidationError("trained_through must be strictly earlier than decision_date")

        vector = self._vector(features)
        p_fill = (
            1.0
            if self.assume_open_fill
            else _sigmoid(self._raw_head("fill", vector))
        )
        conditional_mean = self._raw_head("return_mean", vector)
        q10 = self._raw_head("return_q10", vector)
        q50 = self._raw_head("return_q50", vector)
        q90 = self._raw_head("return_q90", vector)
        p_exit_delay = _sigmoid(self._raw_head("delay", vector))
        expected_delay_days = self._positive(self._raw_head("delay_days", vector)) + float(
            self.heads["delay_days"].get("minimum_output", 0.0)
        )
        p_promotion = _sigmoid(self._raw_head("promotion", vector))
        coverage = _clip(_value(features, "feature_coverage", 0.0), 0.0, 1.0)
        disagreement = _clip(_value(features, "rank_disagreement", 1.0), 0.0, 1.0)
        uncertainty = _clip(
            0.55 * (1.0 - coverage)
            + 0.30 * disagreement
            + 0.15 * _clip(missing_fraction, 0.0, 1.0),
            0.0,
            1.0,
        )
        historical_cvar = _finite(features.get("cvar_loss_10pct"))
        expected_shortfall = max(
            0.0,
            historical_cvar if historical_cvar is not None else 0.0,
            -min(q10, q50, q90),
        )
        return _finalize_prediction(
            ranking=self.ranking,
            features=features,
            missing_fraction=missing_fraction,
            model_id=self.model_id,
            model_stage="LEARNED_CHALLENGER",
            prediction_status="SCORED",
            p_fill=p_fill,
            expected_fill_ratio=1.0 if self.assume_open_fill else p_fill,
            conditional_mean=conditional_mean,
            q10=q10,
            q50=q50,
            q90=q90,
            p_exit_delay=p_exit_delay,
            expected_delay_days=expected_delay_days,
            p_promotion=p_promotion,
            uncertainty=uncertainty,
            expected_shortfall=expected_shortfall,
            force_borda_fallback=force_borda_fallback,
        )


def _rank_with_predictor(
    candidates: list[Candidate],
    ranking_config: dict[str, Any],
    predictor: Any,
    *,
    decision_date: str | None,
) -> list[tuple[Candidate, PredictionBundle]]:
    if not candidates:
        return []

    def missing_fraction(features: dict[str, Any]) -> float:
        """Measure the fields actually consumed by a learned champion.

        The transparent baseline intentionally retains the established market
        quality coverage.  Learned artifacts must not hide missing upstream
        predictors behind their fitted medians, so their complete frozen
        ``feature_order`` is measured before imputation.
        """

        feature_order = getattr(predictor, "feature_order", None)
        if isinstance(feature_order, (tuple, list)) and feature_order:
            missing = sum(_finite(features.get(field)) is None for field in feature_order)
            return missing / len(feature_order)
        coverage = _clip(_value(features, "feature_coverage", 0.0), 0.0, 1.0)
        return 1.0 - coverage

    maximum_missing = float(ranking_config.get("max_missing_fraction", 0.34))
    cohort_fallback = any(
        item.features.get("market_data_valid") is not True
        or missing_fraction(item.features) > maximum_missing
        for item in candidates
    )
    scored: list[tuple[Candidate, PredictionBundle]] = []
    for candidate in candidates:
        candidate_missing_fraction = missing_fraction(candidate.features)
        prediction = predictor.predict(
            candidate.features,
            missing_fraction=candidate_missing_fraction,
            force_borda_fallback=cohort_fallback,
            decision_date=decision_date,
        )
        scored.append((candidate, prediction))

    if cohort_fallback:
        scored.sort(
            key=lambda item: (
                -_value(item[0].features, "rank_borda", 0.0),
                -_value(item[0].features, "rank_consensus", 0.0),
                _value(item[0].features, "rank_disagreement", 1.0),
                item[0].ts_code,
            )
        )
    else:
        scored.sort(
            key=lambda item: (
                -item[1].utility,
                -_value(item[0].features, "rank_consensus", 0.0),
                item[0].ts_code,
            )
        )
    return scored


def rank_with_champion(
    candidates: list[Candidate],
    ranking_config: dict[str, Any],
    *,
    estimated_round_trip_rate: float,
) -> list[tuple[Candidate, PredictionBundle]]:
    """Score a cohort and apply one deterministic ranking regime for the day."""

    champion = TransparentChampionV2(
        ranking_config,
        estimated_round_trip_rate=estimated_round_trip_rate,
    )
    return _rank_with_predictor(
        candidates,
        ranking_config,
        champion,
        decision_date=None,
    )


def rank_with_learned(
    candidates: list[Candidate],
    ranking_config: dict[str, Any],
    challenger: LearnedChallenger,
    *,
    decision_date: str,
) -> list[tuple[Candidate, PredictionBundle]]:
    return _rank_with_predictor(
        candidates,
        ranking_config,
        challenger,
        decision_date=decision_date,
    )


__all__ = [
    "MODEL_ID",
    "LEARNED_ARTIFACT_SCHEMA_VERSION",
    "PREDICTION_SCHEMA_VERSION",
    "ArtifactValidationError",
    "LearnedChallenger",
    "PredictionBundle",
    "TransparentChampionV2",
    "rank_with_champion",
    "rank_with_learned",
]
