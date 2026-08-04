from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .domain import ContractError, normalize_date
from .features import FEATURE_SCHEMA_VERSION
from .model_registry import evaluate_promotion_readiness
from .walk_forward import expanding_walk_forward


MODEL_ARTIFACT_SCHEMA = "model_artifact_v1"
PROMOTION_REPORT_SCHEMA = "model_promotion_report_v1"

# This is intentionally narrower than the upstream D-as-of extraction list.
# Training never flattens arbitrary source values and never infers new columns
# from the observed dataset.
FEATURE_ALLOWLIST = (
    "ret_1d",
    "ret_3d",
    "ret_5d",
    "ret_10d",
    "ret_20d",
    "volatility_5d",
    "volatility_20d",
    "downside_volatility_20d",
    "atr_14d",
    "amplitude_20d",
    "cvar_loss_10pct",
    "max_drawdown_20d",
    "avg_amount_20d",
    "avg_volume_20d",
    "avg_turnover_20d",
    "bar_count",
    "feature_coverage",
    "rank_percentile_a_top10",
    "rank_percentile_premium_top10",
    "rank_percentile_decision_table",
    "rank_borda",
    "rank_consensus",
    "rank_disagreement",
    "stage_from",
    "stage_to",
    "stage_is_2_to_3",
    "stage_is_3_to_4",
    "source_strength",
    "src_a_top10__prob_final",
    "src_a_top10__p_limit_up_calibrated",
    "src_a_top10__auction_strength_score",
    "src_premium_top10__premium_rank_score",
    "src_premium_top10__t_limitup_prob_calibrated",
    "src_premium_top10__t_close_ret_pred",
    "src_premium_top10__t1_accept_prob_blend",
    "src_premium_top10__t1_fail_prob_blend",
    "src_decision_table__decision_p_fill",
    "src_decision_table__decision_e_ret",
    "src_decision_table__decision_ev",
    "src_decision_table__predicted_fill_probability",
    "src_decision_table__predicted_net_return",
    "src_decision_table__predicted_exit_delay_probability",
    "src_decision_table__predicted_continuation_limit_up_probability",
)

PROBABILITY_TARGETS = {
    "fill": "fill",
    "delay": "exit_delayed",
    "promotion": "promotion",
}
RETURN_TARGET = "conditional_net_return"
DELAY_DAYS_TARGET = "exit_delay_days"
MINIMUM_HEAD_SAMPLES = 30
TRAINING_ITERATIONS = 240
L2_PENALTY = 0.02


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _row_id(row: Mapping[str, Any], index: int) -> str:
    value = str(row.get("row_id") or "").strip()
    if not value:
        raise ContractError(f"training row {index} requires row_id")
    return value


def _sorted_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result = [dict(item) for item in rows]
    seen: set[str] = set()
    for index, item in enumerate(result):
        identity = _row_id(item, index)
        if identity in seen:
            raise ContractError(f"duplicate model-training row_id: {identity}")
        seen.add(identity)
        decision_date = normalize_date(item.get("decision_date"), "decision_date")
        feature_asof = item.get("feature_asof")
        if feature_asof is not None and str(feature_asof).strip():
            normalized_asof = normalize_date(feature_asof, "feature_asof")
            if normalized_asof > decision_date:
                raise ContractError("feature_asof cannot be later than decision_date")
        if not isinstance(item.get("features"), Mapping):
            raise ContractError(f"{identity} features must be an object")
        if not isinstance(item.get("labels"), Mapping):
            raise ContractError(f"{identity} labels must be an object")
    return sorted(
        result,
        key=lambda item: (
            normalize_date(item["decision_date"], "decision_date"),
            int(item.get("rank") or 0),
            str(item.get("ts_code") or ""),
            str(item["row_id"]),
        ),
    )


def _mature_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    mature: list[dict[str, Any]] = []
    for row in rows:
        labels = row["labels"]
        if labels.get("is_mature") is not True:
            continue
        label_end = labels.get("label_end_date")
        if label_end is None or not str(label_end).strip():
            raise ContractError("mature model-training row requires label_end_date")
        normalize_date(label_end, "label_end_date")
        mature.append(row)
    return mature


def numeric_feature_rows(
    rows: Iterable[Mapping[str, Any]],
    feature_order: Sequence[str] = FEATURE_ALLOWLIST,
) -> list[list[float | None]]:
    """Extract only explicitly approved finite numeric frozen features."""

    order = tuple(feature_order)
    if not order or len(order) != len(set(order)):
        raise ContractError("feature_order must be nonempty and unique")
    if any(item not in FEATURE_ALLOWLIST for item in order):
        raise ContractError("feature_order contains a non-whitelisted feature")
    matrix: list[list[float | None]] = []
    for row in _sorted_rows(rows):
        features = row["features"]
        matrix.append([_finite(features.get(name)) for name in order])
    return matrix


def fit_normalization(
    rows: Iterable[Mapping[str, Any]],
    feature_order: Sequence[str] = FEATURE_ALLOWLIST,
) -> dict[str, list[float]]:
    ordered = _sorted_rows(rows)
    if not ordered:
        raise ContractError("normalization requires at least one training row")
    matrix = numeric_feature_rows(ordered, feature_order)
    medians: list[float] = []
    means: list[float] = []
    scales: list[float] = []
    for column in range(len(feature_order)):
        observed = [
            float(row[column])
            for row in matrix
            if row[column] is not None
        ]
        median = statistics.median(observed) if observed else 0.0
        imputed = [
            median if row[column] is None else float(row[column])
            for row in matrix
        ]
        mean = statistics.fmean(imputed)
        scale = statistics.pstdev(imputed) if len(imputed) > 1 else 0.0
        if not math.isfinite(scale) or scale < 1e-12:
            scale = 1.0
        medians.append(float(median))
        means.append(float(mean))
        scales.append(float(scale))
    return {"median": medians, "mean": means, "scale": scales}


def transform_rows(
    rows: Iterable[Mapping[str, Any]],
    feature_order: Sequence[str],
    normalization: Mapping[str, Sequence[float]],
) -> list[list[float]]:
    order = tuple(feature_order)
    matrix = numeric_feature_rows(rows, order)
    median = list(normalization.get("median", ()))
    mean = list(normalization.get("mean", ()))
    scale = list(normalization.get("scale", ()))
    if not (len(median) == len(mean) == len(scale) == len(order)):
        raise ContractError("normalization arrays must match feature_order")
    if any(not math.isfinite(float(item)) for item in (*median, *mean, *scale)):
        raise ContractError("normalization values must be finite")
    if any(float(item) <= 0 for item in scale):
        raise ContractError("normalization scales must be positive")
    return [
        [
            (
                (float(median[index]) if value is None else float(value))
                - float(mean[index])
            )
            / float(scale[index])
            for index, value in enumerate(row)
        ]
        for row in matrix
    ]


def _sigmoid(value: float) -> float:
    bounded = max(-35.0, min(35.0, value))
    return 1.0 / (1.0 + math.exp(-bounded))


def _softplus(value: float) -> float:
    if value > 30.0:
        return value
    if value < -30.0:
        return math.exp(value)
    return math.log1p(math.exp(value))


def _linear_predict(
    intercept: float,
    coefficients: Sequence[float],
    values: Sequence[float],
) -> float:
    return intercept + sum(
        weight * value for weight, value in zip(coefficients, values, strict=True)
    )


def _fit_logistic(
    matrix: Sequence[Sequence[float]],
    labels: Sequence[float],
) -> dict[str, Any]:
    width = len(matrix[0])
    coefficients = [0.0] * width
    prevalence = min(1.0 - 1e-6, max(1e-6, statistics.fmean(labels)))
    intercept = math.log(prevalence / (1.0 - prevalence))
    for iteration in range(TRAINING_ITERATIONS):
        rate = 0.12 / math.sqrt(1.0 + iteration / 30.0)
        grad_intercept = 0.0
        gradients = [0.0] * width
        for values, label in zip(matrix, labels, strict=True):
            error = _sigmoid(
                _linear_predict(intercept, coefficients, values)
            ) - label
            grad_intercept += error
            for column, value in enumerate(values):
                gradients[column] += error * value
        count = float(len(labels))
        intercept -= rate * grad_intercept / count
        for column in range(width):
            gradient = gradients[column] / count + L2_PENALTY * coefficients[column]
            coefficients[column] -= rate * gradient
    return {
        "type": "l2_logistic",
        "intercept": intercept,
        "coefficients": coefficients,
        "l2": L2_PENALTY,
        "iterations": TRAINING_ITERATIONS,
    }


def _fit_robust_linear(
    matrix: Sequence[Sequence[float]],
    labels: Sequence[float],
    *,
    loss: str,
    quantile: float | None = None,
) -> dict[str, Any]:
    width = len(matrix[0])
    coefficients = [0.0] * width
    intercept = (
        statistics.median(labels)
        if quantile is None
        else sorted(labels)[
            min(len(labels) - 1, max(0, math.ceil(quantile * len(labels)) - 1))
        ]
    )
    delta = max(0.002, statistics.median(abs(value - intercept) for value in labels) * 1.5)
    for iteration in range(TRAINING_ITERATIONS):
        rate = 0.035 / math.sqrt(1.0 + iteration / 30.0)
        grad_intercept = 0.0
        gradients = [0.0] * width
        for values, label in zip(matrix, labels, strict=True):
            prediction = _linear_predict(intercept, coefficients, values)
            if loss == "huber":
                residual = prediction - label
                gradient = max(-delta, min(delta, residual))
            else:
                if prediction > label:
                    gradient = 1.0 - float(quantile)
                elif prediction < label:
                    gradient = -float(quantile)
                else:
                    gradient = 0.0
            grad_intercept += gradient
            for column, value in enumerate(values):
                gradients[column] += gradient * value
        count = float(len(labels))
        intercept -= rate * grad_intercept / count
        for column in range(width):
            gradient = gradients[column] / count + L2_PENALTY * coefficients[column]
            coefficients[column] -= rate * gradient
    payload = {
        "type": "huber_linear" if loss == "huber" else "pinball_linear",
        "intercept": intercept,
        "coefficients": coefficients,
        "l2": L2_PENALTY,
        "iterations": TRAINING_ITERATIONS,
    }
    if loss == "huber":
        payload["huber_delta"] = delta
    else:
        payload["quantile"] = quantile
    return payload


def _inverse_softplus(value: float, minimum: float = 0.001) -> float:
    # Inference applies ``softplus(raw) + minimum``. Subtract that floor
    # before applying the inverse so fitting and inference use one scale.
    target = max(minimum, value - minimum)
    if target > 30.0:
        return target
    return math.log(math.expm1(target))


def _target_rows(
    rows: Sequence[dict[str, Any]],
    target: str,
) -> tuple[list[dict[str, Any]], list[float]]:
    selected: list[dict[str, Any]] = []
    labels: list[float] = []
    for row in rows:
        value = _finite(row["labels"].get(target))
        if value is None:
            continue
        selected.append(row)
        labels.append(value)
    return selected, labels


def _head_eligibility(
    rows: Sequence[dict[str, Any]],
    minimum_samples: int,
) -> tuple[dict[str, tuple[list[dict[str, Any]], list[float]]], list[str]]:
    datasets: dict[str, tuple[list[dict[str, Any]], list[float]]] = {}
    reasons: list[str] = []
    for head, target in PROBABILITY_TARGETS.items():
        selected, labels = _target_rows(rows, target)
        datasets[head] = (selected, labels)
        if len(labels) < minimum_samples:
            reasons.append(f"{head}_samples_below_minimum")
        elif set(labels) != {0.0, 1.0}:
            reasons.append(f"{head}_requires_two_classes")
    returns = _target_rows(rows, RETURN_TARGET)
    datasets["return"] = returns
    if len(returns[1]) < minimum_samples:
        reasons.append("conditional_return_samples_below_minimum")
    delay_days = _target_rows(rows, DELAY_DAYS_TARGET)
    datasets["delay_days"] = delay_days
    if len(delay_days[1]) < minimum_samples:
        reasons.append("delay_days_samples_below_minimum")
    return datasets, reasons


def _fit_heads(
    rows: Sequence[dict[str, Any]],
    feature_order: Sequence[str],
    normalization: Mapping[str, Sequence[float]],
    *,
    minimum_samples: int,
) -> tuple[dict[str, Any] | None, dict[str, int], list[str]]:
    datasets, reasons = _head_eligibility(rows, minimum_samples)
    counts = {name: len(labels) for name, (_, labels) in datasets.items()}
    if reasons:
        return None, counts, reasons
    heads: dict[str, Any] = {}
    for head in ("fill", "delay", "promotion"):
        selected, labels = datasets[head]
        matrix = transform_rows(selected, feature_order, normalization)
        heads[head] = _fit_logistic(matrix, labels)
    selected, labels = datasets["return"]
    matrix = transform_rows(selected, feature_order, normalization)
    heads["return_mean"] = _fit_robust_linear(matrix, labels, loss="huber")
    for name, quantile in (("q10", 0.10), ("q50", 0.50), ("q90", 0.90)):
        heads[name] = _fit_robust_linear(
            matrix,
            labels,
            loss="pinball",
            quantile=quantile,
        )
    selected, labels = datasets["delay_days"]
    matrix = transform_rows(selected, feature_order, normalization)
    transformed = [_inverse_softplus(value) for value in labels]
    delay_head = _fit_robust_linear(matrix, transformed, loss="huber")
    delay_head.update(
        {
            "type": "positive_linear",
            "link": "softplus",
            "minimum_output": 0.001,
            "target_transform": "inverse_softplus",
        }
    )
    heads["delay_days"] = delay_head
    return heads, counts, []


def _feature_schema(rows: Sequence[dict[str, Any]]) -> tuple[str | None, list[str]]:
    versions = {
        str(item.get("feature_version") or item["features"].get("feature_schema_version") or "").strip()
        for item in rows
    }
    versions.discard("")
    if len(versions) > 1:
        return None, ["mixed_feature_schema_versions"]
    schema = next(iter(versions)) if versions else FEATURE_SCHEMA_VERSION
    if schema != FEATURE_SCHEMA_VERSION:
        return schema, ["unsupported_feature_schema_version"]
    return schema, []


def _dataset_fingerprint(
    rows: Sequence[dict[str, Any]],
    feature_order: Sequence[str],
) -> str:
    evidence = [
        {
            "row_id": item["row_id"],
            "decision_date": item["decision_date"],
            "feature_asof": item.get("feature_asof"),
            "feature_version": item.get("feature_version"),
            "features": {
                name: _finite(item["features"].get(name))
                for name in feature_order
            },
            "labels": item["labels"],
            "source_provenance": item.get("source_provenance"),
            "label_quality": item.get("label_quality"),
        }
        for item in rows
    ]
    return hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def train_challenger(
    rows: Iterable[Mapping[str, Any]],
    *,
    validation_passed: bool = False,
    feature_order: Sequence[str] = FEATURE_ALLOWLIST,
) -> dict[str, Any]:
    """Train all formal heads only after the immutable 180/60/126 gate passes."""

    ordered = _sorted_rows(rows)
    readiness = evaluate_promotion_readiness(ordered)
    if readiness["status"] != "ELIGIBLE_FOR_VALIDATION":
        return {
            "status": "NOT_ELIGIBLE",
            "artifact": None,
            "readiness": readiness,
            "reasons": list(readiness["reasons"]),
        }
    # Pending rows are deliberately excluded from every fitted statistic,
    # checksum and cutoff. Even unlabeled future covariates would otherwise
    # leak into train-time medians and standardization.
    training_rows = _mature_rows(ordered)
    schema, schema_reasons = _feature_schema(training_rows)
    if schema_reasons:
        return {
            "status": "NOT_ELIGIBLE",
            "artifact": None,
            "readiness": readiness,
            "reasons": schema_reasons,
        }
    head_sets, head_reasons = _head_eligibility(
        training_rows,
        MINIMUM_HEAD_SAMPLES,
    )
    if head_reasons:
        return {
            "status": "NOT_ELIGIBLE",
            "artifact": None,
            "readiness": readiness,
            "reasons": head_reasons,
            "head_sample_counts": {
                name: len(labels) for name, (_, labels) in head_sets.items()
            },
        }

    order = tuple(feature_order)
    normalization = fit_normalization(training_rows, order)
    heads, counts, reasons = _fit_heads(
        training_rows,
        order,
        normalization,
        minimum_samples=MINIMUM_HEAD_SAMPLES,
    )
    if heads is None:
        return {
            "status": "NOT_ELIGIBLE",
            "artifact": None,
            "readiness": readiness,
            "reasons": reasons,
            "head_sample_counts": counts,
        }
    trained_through = max(
        normalize_date(item["labels"]["label_end_date"], "label_end_date")
        for item in training_rows
    )
    checksum_inputs = {
        "dataset_sha256": _dataset_fingerprint(training_rows, order),
        "feature_order_sha256": hashlib.sha256(
            json.dumps(list(order), separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "training_rows": len(training_rows),
        "mature_rows": readiness["mature_candidates"],
        "iterations": TRAINING_ITERATIONS,
        "l2": L2_PENALTY,
    }
    identity = hashlib.sha256(
        json.dumps(checksum_inputs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    artifact = {
        "schema": MODEL_ARTIFACT_SCHEMA,
        "schema_version": MODEL_ARTIFACT_SCHEMA,
        "model_id": f"formal_quant_challenger_{trained_through}_{identity}",
        "feature_schema": schema,
        "feature_schema_version": schema,
        "trained_through": trained_through,
        "feature_order": list(order),
        "normalization": normalization,
        "heads": heads,
        # A learned engine must refuse this artifact unless an external,
        # lockbox-aware promotion decision has explicitly passed.
        "validation_passed": validation_passed is True,
        "training_metadata": {
            "head_sample_counts": counts,
            "readiness": readiness,
            "deterministic": True,
        },
        "checksum_inputs": checksum_inputs,
    }
    return {
        "status": "TRAINED_VALIDATED" if validation_passed is True else "TRAINED_UNVALIDATED",
        "artifact": artifact,
        "readiness": readiness,
        "reasons": [] if validation_passed is True else ["validation_not_passed"],
    }


def _validate_artifact(artifact: Mapping[str, Any], *, require_validated: bool) -> None:
    if artifact.get("schema") != MODEL_ARTIFACT_SCHEMA:
        raise ContractError("unsupported model artifact schema")
    order = artifact.get("feature_order")
    normalization = artifact.get("normalization")
    heads = artifact.get("heads")
    if not isinstance(order, list) or not order or len(order) != len(set(order)):
        raise ContractError("artifact feature_order is invalid")
    if any(item not in FEATURE_ALLOWLIST for item in order):
        raise ContractError("artifact contains a non-whitelisted feature")
    if not isinstance(normalization, Mapping):
        raise ContractError("artifact normalization is invalid")
    for key in ("median", "mean", "scale"):
        values = normalization.get(key)
        if not isinstance(values, list) or len(values) != len(order):
            raise ContractError("artifact normalization length mismatch")
    required_heads = {
        "fill": "l2_logistic",
        "delay": "l2_logistic",
        "promotion": "l2_logistic",
        "return_mean": "huber_linear",
        "q10": "pinball_linear",
        "q50": "pinball_linear",
        "q90": "pinball_linear",
        "delay_days": "positive_linear",
    }
    if not isinstance(heads, Mapping) or set(required_heads) - set(heads):
        raise ContractError("artifact heads are incomplete")
    for name, expected_type in required_heads.items():
        head = heads[name]
        if (
            not isinstance(head, Mapping)
            or head.get("type") != expected_type
            or not isinstance(head.get("coefficients"), list)
            or len(head["coefficients"]) != len(order)
            or _finite(head.get("intercept")) is None
            or any(_finite(value) is None for value in head["coefficients"])
        ):
            raise ContractError(f"artifact head {name} is invalid")
    if require_validated and artifact.get("validation_passed") is not True:
        raise ContractError("learned artifact has not passed validation")


def predict_artifact(
    artifact: Mapping[str, Any],
    features: Mapping[str, Any],
    *,
    require_validated: bool = True,
) -> dict[str, float]:
    _validate_artifact(artifact, require_validated=require_validated)
    synthetic = {
        "row_id": "inference",
        "decision_date": "20000101",
        "rank": 0,
        "ts_code": "",
        "features": dict(features),
        "labels": {},
    }
    values = transform_rows(
        [synthetic],
        artifact["feature_order"],
        artifact["normalization"],
    )[0]
    raw: dict[str, float] = {}
    for name, head in artifact["heads"].items():
        raw[name] = _linear_predict(
            float(head["intercept"]),
            [float(value) for value in head["coefficients"]],
            values,
        )
    quantiles = sorted((raw["q10"], raw["q50"], raw["q90"]))
    return {
        "p_fill": _sigmoid(raw["fill"]),
        "p_exit_delay": _sigmoid(raw["delay"]),
        "p_promotion": _sigmoid(raw["promotion"]),
        "conditional_net_return_mean": raw["return_mean"],
        "conditional_net_return_q10": quantiles[0],
        "conditional_net_return_q50": quantiles[1],
        "conditional_net_return_q90": quantiles[2],
        "expected_delay_days": _softplus(raw["delay_days"])
        + float(artifact["heads"]["delay_days"].get("minimum_output", 0.001)),
    }


def probability_metrics(
    observations: Sequence[tuple[float, int]],
    *,
    bins: int = 10,
) -> dict[str, float | int | None]:
    if not observations:
        return {"count": 0, "brier": None, "logloss": None, "ece": None}
    clipped = [
        (min(1.0 - 1e-12, max(1e-12, float(probability))), int(label))
        for probability, label in observations
    ]
    brier = statistics.fmean((probability - label) ** 2 for probability, label in clipped)
    logloss = -statistics.fmean(
        label * math.log(probability) + (1 - label) * math.log(1.0 - probability)
        for probability, label in clipped
    )
    buckets: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for probability, label in clipped:
        buckets[min(bins - 1, int(probability * bins))].append((probability, label))
    ece = sum(
        len(items)
        / len(clipped)
        * abs(
            statistics.fmean(probability for probability, _ in items)
            - statistics.fmean(label for _, label in items)
        )
        for items in buckets.values()
    )
    return {
        "count": len(clipped),
        "brier": brier,
        "logloss": logloss,
        "ece": ece,
    }


def _portfolio_risk(daily_returns: Sequence[float]) -> dict[str, float | None]:
    if not daily_returns:
        return {
            "strategy_after_cost_mean": None,
            "max_drawdown": None,
            "cvar_10pct": None,
            "cvar_loss_10pct": None,
        }
    equity = 1.0
    peak = 1.0
    worst_drawdown = 0.0
    for value in daily_returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        worst_drawdown = min(worst_drawdown, equity / peak - 1.0)
    count = max(1, math.ceil(len(daily_returns) * 0.10))
    cvar = statistics.fmean(sorted(daily_returns)[:count])
    return {
        "strategy_after_cost_mean": statistics.fmean(daily_returns),
        "max_drawdown": worst_drawdown,
        "cvar_10pct": cvar,
        "cvar_loss_10pct": max(0.0, -cvar),
    }


def walk_forward_oof_report(
    rows: Iterable[Mapping[str, Any]],
    *,
    trading_days: Sequence[str] | None = None,
    min_train_days: int = 20,
    validation_days: int = 1,
    embargo_days: int = 0,
    lockbox_days: int = 126,
    feature_order: Sequence[str] = FEATURE_ALLOWLIST,
) -> dict[str, Any]:
    ordered = _sorted_rows(rows)
    folds = expanding_walk_forward(
        ordered,
        min_train_days=min_train_days,
        validation_days=validation_days,
        embargo_days=embargo_days,
        lockbox_days=lockbox_days,
        trading_days=trading_days,
    )
    probability_pairs: dict[str, list[tuple[float, int]]] = {
        "fill": [],
        "delay": [],
        "promotion": [],
    }
    selected_by_day: dict[str, list[float]] = defaultdict(list)
    prediction_count = 0
    skipped_folds: list[dict[str, Any]] = []
    used_folds = 0
    for fold in folds:
        training = [dict(item) for item in fold.train_rows]
        validation = [dict(item) for item in fold.validation_rows]
        normalization = fit_normalization(training, feature_order)
        heads, _, reasons = _fit_heads(
            training,
            feature_order,
            normalization,
            minimum_samples=MINIMUM_HEAD_SAMPLES,
        )
        if heads is None:
            skipped_folds.append(
                {
                    "validation_start": fold.validation_start,
                    "reasons": reasons,
                }
            )
            continue
        artifact = {
            "schema": MODEL_ARTIFACT_SCHEMA,
            "feature_order": list(feature_order),
            "normalization": normalization,
            "heads": heads,
            "validation_passed": True,
        }
        used_folds += 1
        for row in validation:
            prediction = predict_artifact(artifact, row["features"])
            prediction_count += 1
            for head, target in PROBABILITY_TARGETS.items():
                observed = row["labels"].get(target)
                if observed in (0, 1):
                    probability_pairs[head].append(
                        (float(prediction[f"p_{'exit_delay' if head == 'delay' else head}"]), int(observed))
                    )
            actual = _finite(row["labels"].get("policy_net_return"))
            expected = prediction["p_fill"] * prediction["conditional_net_return_mean"]
            if actual is not None and expected > 0.0:
                selected_by_day[normalize_date(row["decision_date"], "decision_date")].append(actual)
    daily_returns = [
        statistics.fmean(selected_by_day[day]) if selected_by_day.get(day) else 0.0
        for day in sorted(
            {
                normalize_date(item["decision_date"], "decision_date")
                for fold in folds
                for item in fold.validation_rows
            }
        )
    ]
    return {
        "schema": "walk_forward_oof_v1",
        "fold_count": len(folds),
        "used_fold_count": used_folds,
        "skipped_folds": skipped_folds,
        "prediction_count": prediction_count,
        "probability_metrics": {
            name: probability_metrics(values)
            for name, values in probability_pairs.items()
        },
        "strategy_metrics": {
            **_portfolio_risk(daily_returns),
            "daily_count": len(daily_returns),
        },
        "lockbox_days": lockbox_days,
        "embargo_days": embargo_days,
    }


def build_promotion_report(
    rows: Iterable[Mapping[str, Any]],
    oof_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ordered = _sorted_rows(rows)
    readiness = evaluate_promotion_readiness(ordered)
    eligible = readiness["status"] == "ELIGIBLE_FOR_VALIDATION"
    return {
        "schema": PROMOTION_REPORT_SCHEMA,
        "status": "PENDING_VALIDATION" if eligible else "NOT_ELIGIBLE",
        "validation_passed": False,
        "readiness": readiness,
        "oof": dict(oof_report) if oof_report is not None else None,
        "checks": {
            "sample_gate": eligible,
            "walk_forward": None,
            "calibration": None,
            "after_cost_return": None,
            "drawdown_and_cvar": None,
            "stress_tests": None,
            "lockbox": None,
        },
        "benchmarks": {},
        "stress_tests": {},
    }


__all__ = [
    "FEATURE_ALLOWLIST",
    "MODEL_ARTIFACT_SCHEMA",
    "PROMOTION_REPORT_SCHEMA",
    "build_promotion_report",
    "fit_normalization",
    "numeric_feature_rows",
    "predict_artifact",
    "probability_metrics",
    "train_challenger",
    "transform_rows",
    "walk_forward_oof_report",
]
