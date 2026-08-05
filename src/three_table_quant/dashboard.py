from __future__ import annotations

import math
from typing import Any

from .candidate_facts import candidate_display_fields as _candidate_display_fields
from .domain import iso_date


def _compound(returns: list[float]) -> float:
    nav = 1.0
    for value in returns:
        nav *= 1.0 + value
    return nav - 1.0


FINAL_CASH_STATUSES = {"NOT_AVAILABLE", "NO_TRADE", "BUY_UNFILLED"}
PORTFOLIO_FINAL_STATUSES = {"CLOSED", "NO_TRADE", "BUY_UNFILLED"}
NONFINAL_STATUSES = {
    "PENDING_BUY",
    "BUY_UNVERIFIABLE",
    "OPEN",
    "EXIT_UNVERIFIABLE",
    "EXIT_DELAYED",
}
ALLOWED_SLOT_STATUSES = FINAL_CASH_STATUSES | NONFINAL_STATUSES | {"CLOSED"}
ALLOWED_PORTFOLIO_STATUSES = PORTFOLIO_FINAL_STATUSES | NONFINAL_STATUSES
T_DAY_VALIDATION_STATUSES = {"PENDING", "UNVERIFIABLE", "VERIFIED"}
GATE_DECISIONS = {"TRADE", "NO_TRADE"}


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _same_number(actual: Any, expected: float | None) -> bool:
    if expected is None:
        return actual is None
    return _finite(actual) and math.isclose(
        float(actual),
        float(expected),
        rel_tol=1e-12,
        abs_tol=1e-12,
    )


def _event_date(value: Any) -> str:
    """Normalize either YYYYMMDD or an event timestamp to an ISO date."""

    digits = "".join(character for character in str(value or "") if character.isdigit())
    if len(digits) < 8:
        raise ValueError(f"event date is not recoverable from {value!r}")
    return iso_date(digits[:8])


def _return_date(signal: dict[str, Any], trade: dict[str, Any] | None) -> str:
    if trade and trade.get("status") == "CLOSED":
        exit_payload = trade.get("exit") or {}
        actual = exit_payload.get("actual_exit_date") or exit_payload.get("actual_exit_at")
        if actual:
            return _event_date(actual)
    return iso_date(signal["exit_date"])


def _result(value: float | None, is_final: bool) -> str:
    if not is_final or value is None:
        return "PENDING"
    if value > 0:
        return "PROFIT"
    if value < 0:
        return "LOSS"
    return "FLAT"


def _t_day_validation(trade: dict[str, Any] | None) -> dict[str, Any]:
    raw = (trade or {}).get("t_day_validation")
    payload = raw if isinstance(raw, dict) else {}
    return {
        "status": str(payload.get("status") or "PENDING").upper(),
        "t_return": payload.get("t_return"),
        "is_limit_up": payload.get("is_limit_up"),
        "is_promoted": payload.get("is_promoted"),
    }


def _validate_t_day_validation(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("t_day_validation must be an object")
    status = payload.get("status")
    if status not in T_DAY_VALIDATION_STATUSES:
        raise ValueError(f"unsupported t_day_validation status: {status}")
    value = payload.get("t_return")
    is_limit_up = payload.get("is_limit_up")
    is_promoted = payload.get("is_promoted")
    if status == "VERIFIED":
        if not _finite(value):
            raise ValueError("VERIFIED t_day_validation requires a finite t_return")
        if not isinstance(is_limit_up, bool) or not isinstance(is_promoted, bool):
            raise ValueError("VERIFIED t_day_validation requires boolean outcomes")
        if is_promoted and not is_limit_up:
            raise ValueError("T-day promotion requires a T-day limit-up hit")
    elif any(item is not None for item in (value, is_limit_up, is_promoted)):
        raise ValueError("non-verified t_day_validation outcomes must remain null")


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if str(item).strip()]


def _signal_engine(signal: dict[str, Any]) -> dict[str, Any]:
    raw = signal.get("ranking_engine")
    payload = raw if isinstance(raw, dict) else {}
    model_id = str(
        payload.get("selected_model_id")
        or payload.get("model_id")
        or signal.get("model_version")
        or "unknown_model"
    )
    return {
        "engine_version": str(
            payload.get("engine_version") or "legacy_ranking_engine_v1"
        ),
        "selected_model_id": model_id,
        "selected_model_kind": str(
            payload.get("selected_model_kind") or "transparent_baseline"
        ),
        "feature_schema_version": str(
            payload.get("feature_schema_version") or "legacy_features_v1"
        ),
        "label_schema_version": str(
            payload.get("label_schema_version") or "executable_labels_v1"
        ),
        "prediction_stage": str(payload.get("prediction_stage") or "D_PRIOR"),
        "training_cutoff": payload.get("training_cutoff"),
        "calibrated": bool(payload.get("calibrated", False)),
        "fallback_active": bool(payload.get("fallback_active", False)),
        "fallback_reason": payload.get("fallback_reason"),
    }


def _candidate_prediction(
    signal: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    metrics = candidate.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    raw = candidate.get("prediction")
    if not isinstance(raw, dict):
        raw = metrics.get("prediction")
    raw = raw if isinstance(raw, dict) else {}
    engine = _signal_engine(signal)

    def first(*keys: str) -> Any:
        for key in keys:
            if key in raw:
                return raw.get(key)
            if key in metrics:
                return metrics.get(key)
        return None

    eligible = metrics.get("policy_trade_eligible")
    gate_decision = str(
        first("gate_decision", "policy_gate")
        or ("TRADE" if eligible is True else "NO_TRADE")
    ).upper()
    gate_reasons = _string_list(first("gate_reasons"))
    if not gate_reasons and gate_decision == "NO_TRADE":
        known = {
            "p_fill_below_threshold",
            "expected_net_return_not_positive",
            "return_lower_bound_not_positive",
            "conditional_return_lcb_not_positive",
            "risk_adjusted_utility_not_positive",
            "exit_delay_probability_too_high",
            "exit_delay_risk_above_threshold",
            "prediction_uncertainty_too_high",
            "market_features_incomplete",
            "insufficient_daily_bars",
            "stale_market_features",
            "invalid_candidate_facts",
            "cohort_ranking_fallback_borda",
        }
        gate_reasons = [
            item
            for item in str(candidate.get("action_reason") or "").split(";")
            if item in known
        ]
    return {
        "model_id": str(first("model_id") or engine["selected_model_id"]),
        "model_kind": str(
            first("model_kind") or engine["selected_model_kind"]
        ),
        "feature_schema_version": str(
            first("feature_schema_version")
            or engine["feature_schema_version"]
        ),
        "prediction_stage": str(
            first("prediction_stage") or engine["prediction_stage"]
        ),
        "as_of": iso_date(signal["decision_date"]),
        "calibrated": bool(
            first("calibrated")
            if first("calibrated") is not None
            else engine["calibrated"]
        ),
        "fill_probability": first(
            "fill_probability",
            "p_fill",
            "p_fill_0925",
        ),
        "conditional_net_return_mean": first(
            "conditional_net_return_mean",
            "expected_net_return",
        ),
        "conditional_net_return_p10": first(
            "conditional_net_return_p10",
            "conditional_net_return_q10",
            "net_return_q10",
            "return_q10",
        ),
        "conditional_net_return_p50": first(
            "conditional_net_return_p50",
            "conditional_net_return_q50",
            "net_return_q50",
            "return_q50",
        ),
        "conditional_net_return_p90": first(
            "conditional_net_return_p90",
            "conditional_net_return_q90",
            "net_return_q90",
            "return_q90",
        ),
        "exit_delay_probability": first(
            "exit_delay_probability",
            "p_exit_delay",
        ),
        "expected_exit_delay_days": first(
            "expected_exit_delay_days",
            "expected_delay_days",
        ),
        "promotion_probability": first(
            "promotion_probability",
            "p_promotion",
        ),
        "expected_shortfall": first(
            "expected_shortfall",
            "cvar_loss_10pct",
        ),
        "uncertainty": first("uncertainty"),
        "risk_adjusted_utility": first(
            "risk_adjusted_utility",
            "utility",
            "utility_score",
        ),
        "feature_coverage": first("feature_coverage"),
        "gate_decision": gate_decision,
        "gate_reasons": gate_reasons,
    }


def _engine_summary(
    state: dict[str, Any],
    current_run: dict[str, Any],
) -> dict[str, Any]:
    latest_signal = max(
        state.get("signals", []),
        key=lambda item: str(item.get("decision_date") or ""),
        default={},
    )
    frozen = _signal_engine(latest_signal) if latest_signal else {}
    raw = current_run.get("ranking_engine")
    payload = raw if isinstance(raw, dict) else {}
    required_candidates = int(payload.get("required_mature_candidates", 180))
    required_per_rank = int(payload.get("required_rank_samples", 60))
    required_lockbox_days = int(payload.get("required_lockbox_days", 126))
    mature_candidates = int(payload.get("mature_candidates", 0))
    lockbox_days = int(payload.get("lockbox_days", 0))
    rank_counts = payload.get("mature_rank_counts")
    rank_counts = rank_counts if isinstance(rank_counts, dict) else {}
    selected_model_id = str(
        payload.get("selected_model_id")
        or frozen.get("selected_model_id")
        or "transparent_shadow_baseline_v1"
    )
    status = str(
        payload.get("status")
        or (
            "CHALLENGER_ACTIVE"
            if payload.get("selected_model_kind") == "learned_challenger"
            else "BASELINE_ACTIVE"
        )
    )
    return {
        "engine_version": str(
            payload.get("engine_version")
            or frozen.get("engine_version")
            or "formal_ranking_engine_v2"
        ),
        "selected_model_id": selected_model_id,
        "selected_model_kind": str(
            payload.get("selected_model_kind")
            or frozen.get("selected_model_kind")
            or "transparent_baseline"
        ),
        "feature_schema_version": str(
            payload.get("feature_schema_version")
            or frozen.get("feature_schema_version")
            or "legacy_features_v1"
        ),
        "label_schema_version": str(
            payload.get("label_schema_version")
            or frozen.get("label_schema_version")
            or "executable_labels_v1"
        ),
        "prediction_stage": str(
            payload.get("prediction_stage")
            or frozen.get("prediction_stage")
            or "D_PRIOR"
        ),
        "status": status,
        "status_label": str(
            payload.get("status_label")
            or (
                "挑战者模型运行"
                if status == "CHALLENGER_ACTIVE"
                else "基线运行 · 样本积累中"
            )
        ),
        "training_cutoff": payload.get(
            "training_cutoff",
            frozen.get("training_cutoff"),
        ),
        "calibrated": bool(
            payload.get("calibrated", frozen.get("calibrated", False))
        ),
        "fallback_active": bool(
            payload.get(
                "fallback_active",
                frozen.get("fallback_active", False),
            )
        ),
        "fallback_reason": payload.get(
            "fallback_reason",
            frozen.get("fallback_reason"),
        ),
        "mature_candidates": mature_candidates,
        "required_mature_candidates": required_candidates,
        "mature_rank_counts": {
            str(rank): int(rank_counts.get(str(rank), rank_counts.get(rank, 0)))
            for rank in (1, 2, 3)
        },
        "required_rank_samples": required_per_rank,
        "lockbox_days": lockbox_days,
        "required_lockbox_days": required_lockbox_days,
        "promotion_eligible": bool(payload.get("promotion_eligible", False)),
        "promotion_reason": str(
            payload.get("promotion_reason")
            or "真实成熟样本尚未达到模型晋级门槛"
        ),
    }


def _validate_prediction(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("candidate prediction must be an object")
    for field in (
        "model_id",
        "model_kind",
        "feature_schema_version",
        "prediction_stage",
        "as_of",
    ):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise ValueError(f"candidate prediction {field} must be non-empty")
    if payload["prediction_stage"] not in {"D_PRIOR", "T_AUCTION"}:
        raise ValueError("candidate prediction stage is unsupported")
    if payload.get("gate_decision") not in GATE_DECISIONS:
        raise ValueError("candidate prediction gate_decision is unsupported")
    reasons = payload.get("gate_reasons")
    if not isinstance(reasons, list) or any(
        not isinstance(item, str) or not item for item in reasons
    ):
        raise ValueError("candidate prediction gate_reasons must be strings")
    if payload["gate_decision"] == "TRADE" and reasons:
        raise ValueError("TRADE prediction cannot contain gate failure reasons")
    if not isinstance(payload.get("calibrated"), bool):
        raise ValueError("candidate prediction calibrated must be boolean")

    probability_fields = (
        "fill_probability",
        "exit_delay_probability",
        "promotion_probability",
        "uncertainty",
        "feature_coverage",
    )
    for field in probability_fields:
        value = payload.get(field)
        if value is not None and (
            not _finite(value) or not 0.0 <= float(value) <= 1.0
        ):
            raise ValueError(f"candidate prediction {field} must be within [0, 1]")
    for field in (
        "conditional_net_return_mean",
        "conditional_net_return_p10",
        "conditional_net_return_p50",
        "conditional_net_return_p90",
        "expected_exit_delay_days",
        "expected_shortfall",
        "risk_adjusted_utility",
    ):
        value = payload.get(field)
        if value is not None and not _finite(value):
            raise ValueError(f"candidate prediction {field} must be finite or null")
    if (
        payload.get("expected_exit_delay_days") is not None
        and float(payload["expected_exit_delay_days"]) < 0
    ):
        raise ValueError("candidate expected_exit_delay_days cannot be negative")
    if (
        payload.get("expected_shortfall") is not None
        and float(payload["expected_shortfall"]) < 0
    ):
        raise ValueError("candidate expected_shortfall cannot be negative")
    quantiles = [
        payload.get("conditional_net_return_p10"),
        payload.get("conditional_net_return_p50"),
        payload.get("conditional_net_return_p90"),
    ]
    present = [item is not None for item in quantiles]
    if any(present) and not all(present):
        raise ValueError("candidate return prediction quantiles must be complete")
    if all(present) and not (
        float(quantiles[0]) <= float(quantiles[1]) <= float(quantiles[2])
    ):
        raise ValueError("candidate return prediction quantiles are unordered")


def _validate_engine(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("dashboard engine must be an object")
    for field in (
        "engine_version",
        "selected_model_id",
        "selected_model_kind",
        "feature_schema_version",
        "label_schema_version",
        "prediction_stage",
        "status",
        "status_label",
        "promotion_reason",
    ):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise ValueError(f"dashboard engine {field} must be non-empty")
    for field in (
        "mature_candidates",
        "required_mature_candidates",
        "required_rank_samples",
        "lockbox_days",
        "required_lockbox_days",
    ):
        value = payload.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"dashboard engine {field} must be nonnegative")
    rank_counts = payload.get("mature_rank_counts")
    if not isinstance(rank_counts, dict) or set(rank_counts) != {"1", "2", "3"}:
        raise ValueError("dashboard engine mature_rank_counts is incomplete")
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in rank_counts.values()
    ):
        raise ValueError("dashboard engine rank counts must be nonnegative")
    for field in ("calibrated", "fallback_active", "promotion_eligible"):
        if not isinstance(payload.get(field), bool):
            raise ValueError(f"dashboard engine {field} must be boolean")


def _portfolio_candidate(
    signal: dict[str, Any],
    candidate: dict[str, Any],
    trade: dict[str, Any] | None,
) -> dict[str, Any]:
    status = str(trade.get("status")) if trade is not None else "MISSING_TRADE"
    is_final = status in PORTFOLIO_FINAL_STATUSES
    if status == "CLOSED":
        net_return = float((trade.get("pnl") or {})["net_return_on_allocated"])
    elif status in {"NO_TRADE", "BUY_UNFILLED"}:
        net_return = 0.0
    else:
        net_return = None
    buy = (trade or {}).get("buy") or {}
    exit_payload = (trade or {}).get("exit") or {}
    return_date = _return_date(signal, trade)
    return {
        "candidate_id": f"{signal['decision_date']}:{candidate['ts_code']}",
        "rank": candidate["rank"],
        "symbol": candidate["ts_code"],
        "name": candidate["name"],
        **_candidate_display_fields(candidate),
        "model": _signal_engine(signal),
        "prediction": _candidate_prediction(signal, candidate),
        "t_day_validation": _t_day_validation(trade),
        "status": status,
        "is_final": is_final,
        "buy_price": buy.get("avg_price"),
        "exit_price": exit_payload.get("avg_price"),
        "net_return": net_return,
        "result": _result(net_return, is_final),
        "return_date": return_date,
    }


def _build_portfolio_metrics(portfolio_daily: list[dict[str, Any]]) -> dict[str, Any]:
    final_rows = sorted(
        (row for row in portfolio_daily if row["is_final"]),
        key=lambda row: (row["return_date"], row["decision_date"]),
    )
    pending_rows = [row for row in portfolio_daily if not row["is_final"]]
    equity = 1.0
    equity_by_decision_date: dict[str, float] = {}
    final_returns: list[float] = []
    for row in final_rows:
        value = float(row["portfolio_return"])
        equity *= 1.0 + value
        final_returns.append(value)
        equity_by_decision_date[row["decision_date"]] = equity

    history = [
        {
            "date": row["return_date"] or row["planned_exit_date"],
            "decision_date": row["decision_date"],
            "planned_exit_date": row["planned_exit_date"],
            "return_date": row["return_date"],
            "candidate_count": row["candidate_count"],
            "final_count": row["final_count"],
            "pending_count": row["pending_count"],
            "profitable_count": row["profitable_count"],
            "portfolio_return": row["portfolio_return"],
            "equity_index": equity_by_decision_date.get(row["decision_date"]),
            "result": row["result"],
            "is_final": row["is_final"],
            "is_provisional": row["is_provisional"],
        }
        for row in sorted(
            portfolio_daily,
            key=lambda item: (
                item["return_date"] or item["planned_exit_date"],
                item["decision_date"],
            ),
        )
    ]

    final_by_month: dict[str, list[float]] = {}
    pending_by_month: dict[str, int] = {}
    for row in final_rows:
        final_by_month.setdefault(row["return_date"][:7], []).append(
            float(row["portfolio_return"])
        )
    for row in pending_rows:
        month = row["planned_exit_date"][:7]
        pending_by_month[month] = pending_by_month.get(month, 0) + 1
    month_keys = sorted(set(final_by_month) | set(pending_by_month), reverse=True)
    by_month = {
        month: {
            "cumulative_return": (
                _compound(final_by_month[month])
                if final_by_month.get(month)
                else None
            ),
            "final_days": len(final_by_month.get(month, [])),
            "pending_days": pending_by_month.get(month, 0),
            "is_provisional": bool(
                final_by_month.get(month) and pending_by_month.get(month, 0)
            ),
        }
        for month in month_keys
    }
    return {
        "cumulative_return": _compound(final_returns) if final_returns else None,
        "final_days": len(final_rows),
        "pending_days": len(pending_rows),
        "is_provisional": bool(final_rows and pending_rows),
        "history": history,
        "by_month": by_month,
    }


def build_dashboard(
    state: dict[str, Any],
    issues: list[dict[str, Any]],
    generated_at: str,
    current_run: dict[str, Any],
    tracked_ranks: list[int],
) -> dict[str, Any]:
    trade_keys = [(item["decision_date"], item["rank"]) for item in state["trades"]]
    if len(trade_keys) != len(set(trade_keys)):
        raise ValueError("duplicate trade for fixed decision-date/rank slot")
    trades_by_key = {
        (item["decision_date"], item["rank"]): item for item in state["trades"]
    }
    expected_trades = {
        (signal["decision_date"], candidate["rank"]): (signal, candidate)
        for signal in state["signals"]
        for candidate in signal.get("candidates", [])
    }
    if set(trades_by_key) != set(expected_trades):
        missing = sorted(set(expected_trades) - set(trades_by_key))
        extra = sorted(set(trades_by_key) - set(expected_trades))
        raise ValueError(
            f"all-candidate shadow ledger mismatch; missing={missing}, extra={extra}"
        )
    for key, (signal, candidate) in expected_trades.items():
        trade = trades_by_key[key]
        if (
            trade.get("trade_id") != f"{signal['decision_date']}:R{candidate['rank']}"
            or trade.get("signal_id") != signal.get("signal_id")
            or trade.get("ts_code") != candidate.get("ts_code")
            or trade.get("name") != candidate.get("name")
            or trade.get("buy_date") != signal.get("buy_date")
            or trade.get("planned_exit_date") != signal.get("exit_date")
        ):
            raise ValueError(f"shadow trade identity mismatch for {key}")
    days: list[dict[str, Any]] = []
    rank_daily: list[dict[str, Any]] = []
    portfolio_daily: list[dict[str, Any]] = []
    for signal in sorted(state["signals"], key=lambda item: item["decision_date"]):
        candidates = signal.get("candidates", [])
        slots: dict[str, Any] = {}
        daily_ranks: dict[str, Any] = {}
        for rank in tracked_ranks:
            trade = trades_by_key.get((signal["decision_date"], rank))
            if trade is None:
                slot = {
                    "candidate_id": None,
                    "status": "NOT_AVAILABLE",
                    "reason": "NO_CANDIDATE_FOR_FIXED_RANK",
                    "buy": None,
                    "exit": None,
                    "pnl": None,
                    "t_day_validation": None,
                }
                daily_return = 0.0
                is_final = True
                state_label = "CASH"
            else:
                slot = {
                    "candidate_id": f"{signal['decision_date']}:{trade['ts_code']}",
                    "status": trade["status"],
                    "reason": trade.get("reason"),
                    "buy": trade.get("buy"),
                    "exit": trade.get("exit"),
                    "pnl": trade.get("pnl"),
                    "diagnostics": trade.get("diagnostics", {}),
                    "t_day_validation": _t_day_validation(trade),
                }
                if trade["status"] == "CLOSED":
                    daily_return = float(trade["pnl"]["net_return_on_allocated"])
                    is_final = True
                    state_label = "CLOSED"
                elif trade["status"] in {"NO_TRADE", "BUY_UNFILLED"}:
                    daily_return = 0.0
                    is_final = True
                    state_label = "CASH"
                else:
                    daily_return = None
                    is_final = False
                    state_label = trade["status"]
            slots[str(rank)] = slot
            daily_ranks[str(rank)] = {
                "state": state_label,
                "is_final": is_final,
                "daily_return": daily_return,
                "return_date": _return_date(signal, trade),
            }
        days.append(
            {
                "decision_date": iso_date(signal["decision_date"]),
                "buy_date": iso_date(signal["buy_date"]),
                "planned_exit_date": iso_date(signal["exit_date"]),
                "selection_status": signal["status"],
                "model": _signal_engine(signal),
                "source_snapshots": signal.get("source_snapshots", []),
                "market_data_provenance": signal.get("market_data_provenance", {}),
                "intersection_count": len(candidates),
                "candidates": [
                    {
                        "candidate_id": f"{signal['decision_date']}:{item['ts_code']}",
                        "symbol": item["ts_code"],
                        "name": item["name"],
                        **_candidate_display_fields(item),
                        "model": _signal_engine(signal),
                        "prediction": _candidate_prediction(signal, item),
                        "t_day_validation": _t_day_validation(
                            trades_by_key.get(
                                (signal["decision_date"], item.get("rank"))
                            )
                        ),
                        "rank": item.get("rank"),
                        "model_score": item.get("metrics", {}).get("utility_score"),
                        "action": item.get("action"),
                        "action_reason": item.get("action_reason"),
                        "action_audit": item.get("action_audit", []),
                        "policy_decision": item.get("policy_decision", {}),
                        "source_ranks": item.get("source_ranks", {}),
                        "metrics": item.get("metrics", {}),
                        "features": item.get("features", {}),
                    }
                    for item in candidates
                ],
                "rank_slots": slots,
            }
        )
        portfolio_candidates = [
            _portfolio_candidate(
                signal,
                candidate,
                trades_by_key.get((signal["decision_date"], candidate["rank"])),
            )
            for candidate in candidates
        ]
        final_count = sum(item["is_final"] for item in portfolio_candidates)
        pending_count = len(portfolio_candidates) - final_count
        profitable_count = sum(
            item["is_final"] and float(item["net_return"]) > 0
            for item in portfolio_candidates
        )
        is_final = not portfolio_candidates or pending_count == 0
        is_provisional = bool(final_count and pending_count)
        if not portfolio_candidates:
            portfolio_return = 0.0
            portfolio_return_date = iso_date(signal["exit_date"])
        elif is_final:
            portfolio_return = sum(
                float(item["net_return"]) for item in portfolio_candidates
            ) / len(portfolio_candidates)
            portfolio_return_date = max(
                item["return_date"] for item in portfolio_candidates
            )
        else:
            portfolio_return = None
            portfolio_return_date = None
        portfolio_daily.append(
            {
                "decision_date": iso_date(signal["decision_date"]),
                "buy_date": iso_date(signal["buy_date"]),
                "planned_exit_date": iso_date(signal["exit_date"]),
                "return_date": portfolio_return_date,
                "candidate_count": len(portfolio_candidates),
                "final_count": final_count,
                "pending_count": pending_count,
                "profitable_count": profitable_count,
                "portfolio_return": portfolio_return,
                "result": _result(portfolio_return, is_final),
                "is_final": is_final,
                "is_provisional": is_provisional,
                "model": _signal_engine(signal),
                "candidates": portfolio_candidates,
            }
        )
        rank_daily.append(
            {
                "date": iso_date(signal["exit_date"]),
                "decision_date": iso_date(signal["decision_date"]),
                "ranks": daily_ranks,
            }
        )

    rank_daily.sort(key=lambda item: (item["date"], item["decision_date"]))
    metrics: dict[str, Any] = {}
    for rank in tracked_ranks:
        key = str(rank)
        equity = 1.0
        values: list[float] = []
        closed_values: list[float] = []
        pending_days = 0
        ordered_rows = sorted(
            rank_daily,
            key=lambda row: (row["ranks"][key]["return_date"], row["decision_date"]),
        )
        for row in ordered_rows:
            item = row["ranks"][key]
            value = item["daily_return"]
            if value is not None:
                equity *= 1.0 + value
                values.append(value)
            else:
                pending_days += 1
            item["equity_index"] = equity
            if item["state"] == "CLOSED":
                closed_values.append(float(value))
        metrics[key] = {
            "cumulative_return": _compound(values) if values else None,
            "final_days": len(values),
            "pending_days": pending_days,
            "is_provisional": bool(values and pending_days),
            "closed_trades": len(closed_values),
            "win_rate": (
                sum(value > 0 for value in closed_values) / len(closed_values)
                if closed_values
                else None
            ),
        }

    portfolio_metrics = _build_portfolio_metrics(portfolio_daily)
    months = sorted(
        {
            item["return_date"][:7]
            for row in rank_daily
            for item in row["ranks"].values()
        }
        | set(portfolio_metrics["by_month"]),
        reverse=True,
    )
    return {
        "schema_version": "dashboard_v1",
        "generated_at": generated_at,
        "timezone": "Asia/Shanghai",
        "return_unit": "decimal",
        "policy": {
            "intersection": "ALL_THREE_ACTUAL_ROWS",
            "tracked_ranks": tracked_ranks,
            "allow_backfill": False,
            "execution_mode": "SHADOW_ONLY",
            "entry": "T 09:25 opening call auction exact fill truth required",
            "exit": "T+1 11:00-11:05 five one-minute slices",
        },
        "engine": _engine_summary(state, current_run),
        "current_run": current_run,
        "source_issues": issues,
        "available_months": months,
        "rank_metrics": metrics,
        "portfolio_metrics": portfolio_metrics,
        "days": days,
        "rank_daily": rank_daily,
        "portfolio_daily": portfolio_daily,
    }


def validate_dashboard(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != "dashboard_v1":
        raise ValueError("dashboard schema mismatch")
    _validate_engine(payload.get("engine"))
    tracked = payload["policy"]["tracked_ranks"]
    if tracked != [1, 2, 3]:
        raise ValueError("dashboard must track fixed ranks 1, 2, and 3")
    expected_rank_keys = {str(rank) for rank in tracked}
    seen_decision_dates: set[str] = set()
    days_by_decision_date: dict[str, dict[str, Any]] = {}
    for day in payload["days"]:
        decision_date = day["decision_date"]
        if decision_date in seen_decision_dates:
            raise ValueError("duplicate dashboard decision_date")
        seen_decision_dates.add(decision_date)
        days_by_decision_date[decision_date] = day
        if day["intersection_count"] != len(day["candidates"]):
            raise ValueError("intersection_count does not match candidates")
        candidate_ranks = [item["rank"] for item in day["candidates"]]
        if candidate_ranks != list(range(1, len(candidate_ranks) + 1)):
            raise ValueError("candidate ranks must be contiguous and deterministic")
        candidate_ids = [item["candidate_id"] for item in day["candidates"]]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate ids must be unique within a decision date")
        for candidate in day["candidates"]:
            for field in ("stage_transition", "industry"):
                value = candidate.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"candidate {field} must be a non-empty display string")
            d_close = candidate.get("d_close")
            if d_close is not None and (not _finite(d_close) or float(d_close) <= 0):
                raise ValueError("candidate d_close must be positive when present")
            if candidate.get("action") != "SHADOW":
                raise ValueError("every intersection candidate must remain SHADOW")
            _validate_prediction(candidate.get("prediction"))
            _validate_t_day_validation(candidate.get("t_day_validation"))
            eligible = candidate.get("metrics", {}).get("policy_trade_eligible")
            if not isinstance(eligible, bool):
                raise ValueError("policy_trade_eligible must remain independently recorded")
        if day["intersection_count"] == 0:
            if day["selection_status"] != "NO_CANDIDATE":
                raise ValueError("zero intersection must be NO_CANDIDATE")
        elif day["selection_status"] != "RANKED":
            raise ValueError("non-empty intersection must be RANKED")
        if set(day["rank_slots"]) != expected_rank_keys:
            raise ValueError("fixed rank slots are incomplete")
        candidates_by_rank = {item["rank"]: item for item in day["candidates"]}
        for rank in tracked:
            slot = day["rank_slots"][str(rank)]
            status = slot.get("status")
            if status not in ALLOWED_SLOT_STATUSES:
                raise ValueError(f"unsupported fixed-rank status: {status}")
            candidate = candidates_by_rank.get(rank)
            if candidate is None:
                if status != "NOT_AVAILABLE" or slot.get("candidate_id") is not None:
                    raise ValueError("missing fixed rank must remain NOT_AVAILABLE")
            elif slot.get("candidate_id") != candidate["candidate_id"]:
                raise ValueError("fixed-rank slot candidate does not match ranked candidate")
            if candidate is not None:
                _validate_t_day_validation(slot.get("t_day_validation"))
                if slot.get("t_day_validation") != candidate.get("t_day_validation"):
                    raise ValueError(
                        "fixed-rank T-day validation does not match ranked candidate"
                    )
            if status in {"NOT_AVAILABLE", "NO_TRADE"}:
                if any(slot.get(field) is not None for field in ("buy", "exit", "pnl")):
                    raise ValueError(f"{status} slot cannot contain execution data")
            elif status == "BUY_UNFILLED":
                buy = slot.get("buy") or {}
                if buy.get("filled_qty") != 0 or slot.get("exit") is not None or slot.get("pnl") is not None:
                    raise ValueError("BUY_UNFILLED slot has inconsistent execution data")
            elif status == "CLOSED":
                buy = slot.get("buy") or {}
                exit_payload = slot.get("exit") or {}
                pnl = slot.get("pnl") or {}
                if not buy or exit_payload.get("remaining_qty") != 0:
                    raise ValueError("CLOSED slot must have a fully exited position")
                if not _finite(pnl.get("net_return_on_allocated")):
                    raise ValueError("CLOSED slot must have a finite allocated return")
            elif status == "EXIT_DELAYED":
                exit_payload = slot.get("exit") or {}
                if not slot.get("buy") or int(exit_payload.get("remaining_qty") or 0) <= 0:
                    raise ValueError("EXIT_DELAYED slot must retain an open quantity")

    current_run = payload.get("current_run")
    if not isinstance(current_run, dict):
        raise ValueError("current_run must be an object")
    if "completed" in current_run:
        completed = current_run.get("completed")
        if not isinstance(completed, bool):
            raise ValueError("current_run.completed must be boolean")
        if completed:
            decision_date = current_run.get("decision_date")
            completed_at = current_run.get("completed_at")
            outcome = current_run.get("outcome")
            if not isinstance(completed_at, str) or "T" not in completed_at:
                raise ValueError("completed current_run requires completed_at")
            if decision_date not in days_by_decision_date:
                raise ValueError("completed current_run must reference a dashboard day")
            day = days_by_decision_date[decision_date]
            if current_run.get("status") != day.get("selection_status"):
                raise ValueError("current_run status must match the completed day")
            if current_run.get("intersection_count") != day.get("intersection_count"):
                raise ValueError("current_run intersection count must match the completed day")
            if day["intersection_count"] == 0:
                if outcome != "COMPLETED_ZERO_INTERSECTION":
                    raise ValueError("zero intersection requires completed-zero outcome")
            elif outcome not in {"COMPLETED_RANKED", "COMPLETED_FROZEN_SIGNAL"}:
                raise ValueError("ranked run requires a completed ranking outcome")
        elif current_run.get("completed_at") is not None:
            raise ValueError("incomplete current_run cannot have completed_at")

    seen_rank_decision_dates: set[str] = set()
    for row in payload["rank_daily"]:
        if row.get("decision_date") in seen_rank_decision_dates:
            raise ValueError("duplicate rank_daily decision_date")
        seen_rank_decision_dates.add(row.get("decision_date"))
        day = days_by_decision_date.get(row.get("decision_date"))
        if day is None or row["date"] != day["planned_exit_date"]:
            raise ValueError("rank_daily row is not linked to its decision day")
        if set(row["ranks"]) != expected_rank_keys:
            raise ValueError("rank_daily fixed slots are incomplete")
        for rank in tracked:
            key = str(rank)
            item = row["ranks"][key]
            slot = day["rank_slots"][key]
            value = item.get("daily_return")
            is_final = item.get("is_final")
            return_date = item.get("return_date")
            if not isinstance(return_date, str) or len(return_date) != 10:
                raise ValueError("rank_daily return_date must be an ISO date")
            if not isinstance(is_final, bool):
                raise ValueError("rank_daily is_final must be boolean")
            if is_final:
                if not _finite(value):
                    raise ValueError("final rank day must contain a finite return")
            elif value is not None:
                raise ValueError("non-final rank day must keep daily_return null")
            status = slot["status"]
            if status == "CLOSED":
                expected = float(slot["pnl"]["net_return_on_allocated"])
                if item["state"] != "CLOSED" or not is_final or not math.isclose(float(value), expected):
                    raise ValueError("CLOSED slot and rank_daily return disagree")
                exit_payload = slot.get("exit") or {}
                actual = exit_payload.get("actual_exit_date") or exit_payload.get("actual_exit_at")
                expected_date = _event_date(actual) if actual else day["planned_exit_date"]
                if return_date != expected_date:
                    raise ValueError("CLOSED return must be attributed to its actual exit date")
            elif status in FINAL_CASH_STATUSES:
                if item["state"] != "CASH" or not is_final or float(value) != 0.0:
                    raise ValueError("final cash slot must have a numeric zero return")
                if return_date != day["planned_exit_date"]:
                    raise ValueError("cash result must remain on the planned exit date")
            elif item["state"] != status or is_final or value is not None:
                raise ValueError("pending slot and rank_daily state disagree")
            elif return_date != day["planned_exit_date"]:
                raise ValueError("pending result must remain on its planned exit date")

    for rank in tracked:
        key = str(rank)
        running_equity = 1.0
        ordered_rows = sorted(
            payload["rank_daily"],
            key=lambda row: (row["ranks"][key]["return_date"], row["decision_date"]),
        )
        for row in ordered_rows:
            item = row["ranks"][key]
            if item["is_final"]:
                running_equity *= 1.0 + float(item["daily_return"])
            if not _finite(item.get("equity_index")) or not math.isclose(
                float(item["equity_index"]),
                running_equity,
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                raise ValueError("rank equity index does not match compounded final returns")

    rank_metrics = payload.get("rank_metrics")
    if not isinstance(rank_metrics, dict) or set(rank_metrics) != expected_rank_keys:
        raise ValueError("rank_metrics must contain exactly the fixed ranks")
    for rank in tracked:
        key = str(rank)
        values: list[float] = []
        closed_values: list[float] = []
        pending_days = 0
        ordered_rows = sorted(
            payload["rank_daily"],
            key=lambda row: (row["ranks"][key]["return_date"], row["decision_date"]),
        )
        for row in ordered_rows:
            item = row["ranks"][key]
            if item["is_final"]:
                values.append(float(item["daily_return"]))
            else:
                pending_days += 1
            if item["state"] == "CLOSED":
                closed_values.append(float(item["daily_return"]))
        metric = rank_metrics[key]
        expected_cumulative = _compound(values) if values else None
        actual_cumulative = metric.get("cumulative_return")
        if expected_cumulative is None:
            cumulative_matches = actual_cumulative is None
        else:
            cumulative_matches = _finite(actual_cumulative) and math.isclose(
                float(actual_cumulative),
                expected_cumulative,
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
        expected_win_rate = (
            sum(value > 0 for value in closed_values) / len(closed_values)
            if closed_values
            else None
        )
        actual_win_rate = metric.get("win_rate")
        if expected_win_rate is None:
            win_rate_matches = actual_win_rate is None
        else:
            win_rate_matches = _finite(actual_win_rate) and math.isclose(
                float(actual_win_rate),
                expected_win_rate,
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
        if not cumulative_matches:
            raise ValueError("rank cumulative return does not match final observations")
        if metric.get("final_days") != len(values) or metric.get("pending_days") != pending_days:
            raise ValueError("rank final and pending counts do not match observations")
        if metric.get("is_provisional") is not bool(values and pending_days):
            raise ValueError("rank provisional state does not match observations")
        if metric.get("closed_trades") != len(closed_values) or not win_rate_matches:
            raise ValueError("rank win rate must use CLOSED observations only")

    portfolio_daily = payload.get("portfolio_daily")
    if not isinstance(portfolio_daily, list) or len(portfolio_daily) != len(payload["days"]):
        raise ValueError("portfolio_daily must contain exactly one row per signal")
    if [row.get("decision_date") for row in portfolio_daily] != sorted(
        days_by_decision_date
    ):
        raise ValueError("portfolio_daily must be uniquely ordered by decision_date")
    seen_portfolio_decisions: set[str] = set()
    for row in portfolio_daily:
        decision_date = row.get("decision_date")
        if decision_date in seen_portfolio_decisions:
            raise ValueError("duplicate portfolio_daily decision_date")
        seen_portfolio_decisions.add(decision_date)
        day = days_by_decision_date.get(decision_date)
        if day is None:
            raise ValueError("portfolio_daily row is not linked to its decision day")
        if (
            row.get("buy_date") != day["buy_date"]
            or row.get("planned_exit_date") != day["planned_exit_date"]
        ):
            raise ValueError("portfolio_daily dates do not match the signal")
        details = row.get("candidates")
        if not isinstance(details, list):
            raise ValueError("portfolio candidates must be a list")
        expected_candidates = day["candidates"]
        if row.get("candidate_count") != len(details) or len(details) != len(expected_candidates):
            raise ValueError("portfolio candidate_count does not match strict intersection")
        if [item.get("rank") for item in details] != [
            item["rank"] for item in expected_candidates
        ]:
            raise ValueError("portfolio candidates must preserve deterministic rank order")

        final_count = 0
        profitable_count = 0
        candidate_returns: list[float] = []
        for detail, candidate in zip(details, expected_candidates, strict=True):
            if (
                detail.get("candidate_id") != candidate["candidate_id"]
                or detail.get("symbol") != candidate["symbol"]
                or detail.get("name") != candidate["name"]
            ):
                raise ValueError("portfolio candidate identity does not match ranked candidate")
            if (
                detail.get("stage_transition") != candidate["stage_transition"]
                or detail.get("industry") != candidate["industry"]
                or not _same_number(detail.get("d_close"), candidate.get("d_close"))
            ):
                raise ValueError("portfolio candidate display fields do not match ranked candidate")
            _validate_prediction(detail.get("prediction"))
            if detail.get("prediction") != candidate.get("prediction"):
                raise ValueError(
                    "portfolio candidate prediction does not match ranked candidate"
                )
            _validate_t_day_validation(detail.get("t_day_validation"))
            if detail.get("t_day_validation") != candidate.get("t_day_validation"):
                raise ValueError(
                    "portfolio candidate T-day validation does not match ranked candidate"
                )
            status = detail.get("status")
            if status not in ALLOWED_PORTFOLIO_STATUSES:
                raise ValueError(f"unsupported portfolio candidate status: {status}")
            return_date = detail.get("return_date")
            if not isinstance(return_date, str) or len(return_date) != 10:
                raise ValueError("portfolio candidate return_date must be an ISO date")
            for price_field in ("buy_price", "exit_price"):
                price = detail.get(price_field)
                if price is not None and (not _finite(price) or float(price) <= 0):
                    raise ValueError(f"portfolio {price_field} must be positive when present")
            expected_final = status in PORTFOLIO_FINAL_STATUSES
            if detail.get("is_final") is not expected_final:
                raise ValueError("portfolio candidate final state disagrees with status")
            value = detail.get("net_return")
            if status == "CLOSED":
                if not _finite(value):
                    raise ValueError("CLOSED portfolio candidate needs a finite return")
            elif status in {"NO_TRADE", "BUY_UNFILLED"}:
                if not _same_number(value, 0.0):
                    raise ValueError("final cash portfolio candidate must return zero")
            elif value is not None:
                raise ValueError("pending portfolio candidate must keep net_return null")
            expected_result = _result(
                float(value) if value is not None else None,
                expected_final,
            )
            if detail.get("result") != expected_result:
                raise ValueError("portfolio candidate result disagrees with return")
            if expected_final:
                final_count += 1
                numeric = float(value)
                candidate_returns.append(numeric)
                profitable_count += numeric > 0
            if detail["rank"] in tracked:
                slot = day["rank_slots"][str(detail["rank"])]
                if slot["status"] != status:
                    raise ValueError("portfolio candidate and fixed-rank slot status disagree")

        pending_count = len(details) - final_count
        expected_final = not details or pending_count == 0
        expected_provisional = bool(final_count and pending_count)
        if (
            row.get("final_count") != final_count
            or row.get("pending_count") != pending_count
            or row.get("profitable_count") != profitable_count
        ):
            raise ValueError("portfolio daily counts do not match candidate observations")
        if row.get("is_final") is not expected_final:
            raise ValueError("portfolio daily final state is inconsistent")
        if row.get("is_provisional") is not expected_provisional:
            raise ValueError("portfolio daily provisional state is inconsistent")
        expected_return = (
            0.0
            if not details
            else sum(candidate_returns) / len(details)
            if expected_final
            else None
        )
        if not _same_number(row.get("portfolio_return"), expected_return):
            raise ValueError("portfolio return must be an all-candidate equal-weight average")
        expected_result = _result(expected_return, expected_final)
        if row.get("result") != expected_result:
            raise ValueError("portfolio daily result disagrees with return")
        if not details:
            expected_return_date = day["planned_exit_date"]
        elif expected_final:
            expected_return_date = max(item["return_date"] for item in details)
        else:
            expected_return_date = None
        if row.get("return_date") != expected_return_date:
            raise ValueError("portfolio return_date must be the last candidate completion date")

    portfolio_metrics = payload.get("portfolio_metrics")
    if not isinstance(portfolio_metrics, dict):
        raise ValueError("portfolio_metrics must be an object")
    expected_portfolio_metrics = _build_portfolio_metrics(portfolio_daily)
    if not _same_number(
        portfolio_metrics.get("cumulative_return"),
        expected_portfolio_metrics["cumulative_return"],
    ):
        raise ValueError("portfolio cumulative return does not match final days")
    for field in ("final_days", "pending_days", "is_provisional"):
        if portfolio_metrics.get(field) != expected_portfolio_metrics[field]:
            raise ValueError(f"portfolio metric {field} does not match observations")
    history = portfolio_metrics.get("history")
    expected_history = expected_portfolio_metrics["history"]
    if not isinstance(history, list) or len(history) != len(expected_history):
        raise ValueError("portfolio history must contain every signal day")
    numeric_history_fields = {"portfolio_return", "equity_index"}
    for actual, expected in zip(history, expected_history, strict=True):
        if set(actual) != set(expected):
            raise ValueError("portfolio history schema mismatch")
        for field, expected_value in expected.items():
            if field in numeric_history_fields:
                if not _same_number(actual.get(field), expected_value):
                    raise ValueError(f"portfolio history {field} mismatch")
            elif actual.get(field) != expected_value:
                raise ValueError(f"portfolio history {field} mismatch")
    by_month = portfolio_metrics.get("by_month")
    expected_by_month = expected_portfolio_metrics["by_month"]
    if not isinstance(by_month, dict) or set(by_month) != set(expected_by_month):
        raise ValueError("portfolio monthly buckets do not match observations")
    for month, expected in expected_by_month.items():
        actual = by_month[month]
        if not isinstance(actual, dict) or set(actual) != set(expected):
            raise ValueError("portfolio monthly schema mismatch")
        if not _same_number(
            actual.get("cumulative_return"),
            expected["cumulative_return"],
        ):
            raise ValueError("portfolio monthly return does not match final days")
        for field in ("final_days", "pending_days", "is_provisional"):
            if actual.get(field) != expected[field]:
                raise ValueError(f"portfolio monthly {field} mismatch")

    expected_months = sorted(
        {
            item["return_date"][:7]
            for row in payload["rank_daily"]
            for item in row["ranks"].values()
        }
        | set(expected_by_month),
        reverse=True,
    )
    if payload.get("available_months") != expected_months:
        raise ValueError("available_months does not match rank_daily")
