from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .domain import ContractError, normalize_date, normalize_ts_code


TRAINING_DATASET_SCHEMA = "training_dataset_v1"
FINAL_TRADE_STATUSES = frozenset({"CLOSED", "BUY_UNFILLED"})


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


def _optional_date(value: Any, field_name: str) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return normalize_date(value, field_name)


def _event_date(value: Any, field_name: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ContractError(f"{field_name} is required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{field_name} must be an ISO-8601 timestamp") from exc
    return parsed.strftime("%Y%m%d")


def _closed_exit_date(trade: Mapping[str, Any]) -> str:
    exit_payload = trade.get("exit")
    if not isinstance(exit_payload, Mapping):
        raise ContractError("CLOSED trade requires an exit payload")
    actual_date = _optional_date(
        exit_payload.get("actual_exit_date"),
        "exit.actual_exit_date",
    )
    event_at = exit_payload.get("actual_exit_at")
    event_date = _event_date(event_at, "exit.actual_exit_at") if event_at else None
    if actual_date is None and event_date is None:
        raise ContractError("CLOSED trade requires an actual exit date")
    if actual_date is not None and event_date is not None and actual_date != event_date:
        raise ContractError("actual_exit_date and actual_exit_at disagree")
    return actual_date or str(event_date)


def _source_provenance(signal: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    snapshots = signal.get("source_snapshots", [])
    if not isinstance(snapshots, list):
        raise ContractError("source_snapshots must be a list")
    for item in snapshots:
        if not isinstance(item, Mapping):
            raise ContractError("source snapshot must be an object")
        source_id = str(item.get("source_id") or "").strip()
        if not source_id or source_id in result:
            raise ContractError("source snapshots require unique source_id values")
        result[source_id] = {
            "repository_commit_sha": item.get("repository_commit_sha"),
            "content_sha256": item.get("content_sha256"),
            "generated_at": item.get("generated_at"),
            "decision_date": item.get("decision_date"),
        }
    return result


def _feature_version(signal: Mapping[str, Any], candidate: Mapping[str, Any]) -> Any:
    if "feature_version" in candidate:
        return candidate.get("feature_version")
    if "feature_version" in signal:
        return signal.get("feature_version")
    features = candidate.get("features")
    if isinstance(features, Mapping):
        return features.get("feature_version") or features.get("feature_schema_version")
    return None


def _promotion_label(trade: Mapping[str, Any]) -> tuple[int | None, str | None]:
    validation = trade.get("t_day_validation")
    if not isinstance(validation, Mapping) or validation.get("status") != "VERIFIED":
        return None, None
    value = validation.get("is_promoted")
    if not isinstance(value, bool):
        raise ContractError("VERIFIED T-day validation requires boolean is_promoted")
    return int(value), normalize_date(
        validation.get("trade_date") or trade.get("buy_date"),
        "t_day_validation.trade_date",
    )


def _fill_label(trade: Mapping[str, Any]) -> tuple[int | None, str | None]:
    status = str(trade.get("status") or "")
    buy = trade.get("buy")
    if status == "BUY_UNFILLED":
        if not isinstance(buy, Mapping) or buy.get("filled_qty") != 0:
            raise ContractError("BUY_UNFILLED requires an explicit zero-fill record")
        return 0, normalize_date(trade.get("buy_date"), "buy_date")
    if isinstance(buy, Mapping):
        filled_qty = buy.get("filled_qty")
        if filled_qty is not None and _finite_number(filled_qty, "buy.filled_qty") > 0:
            return 1, normalize_date(trade.get("buy_date"), "buy_date")
    if status in {"OPEN", "EXIT_DELAYED", "EXIT_UNVERIFIABLE", "CLOSED"}:
        raise ContractError(f"{status} requires a positive filled quantity")
    return None, None


def _labels(trade: Mapping[str, Any]) -> dict[str, Any]:
    status = str(trade.get("status") or "")
    fill, fill_end = _fill_label(trade)
    promoted, promotion_end = _promotion_label(trade)
    conditional_return: float | None = None
    policy_return: float | None = None
    return_end: str | None = None
    delayed: int | None = None
    delay_days: int | None = None

    if status == "CLOSED":
        pnl = trade.get("pnl")
        if not isinstance(pnl, Mapping):
            raise ContractError("CLOSED trade requires pnl")
        conditional_return = _finite_number(
            pnl.get("net_return_on_allocated"),
            "pnl.net_return_on_allocated",
        )
        policy_return = conditional_return
        return_end = _closed_exit_date(trade)
        exit_payload = trade.get("exit")
        raw_delay = exit_payload.get("delay_trading_days") if isinstance(exit_payload, Mapping) else None
        if raw_delay is None:
            raise ContractError("CLOSED trade requires delay_trading_days")
        delay_value = _finite_number(raw_delay, "exit.delay_trading_days")
        if delay_value < 0 or not delay_value.is_integer():
            raise ContractError("exit.delay_trading_days must be a nonnegative integer")
        delay_days = int(delay_value)
        delayed = int(delay_days > 0)
    elif status == "BUY_UNFILLED":
        # Zero is known policy cash, not a conditional return observation.
        policy_return = 0.0

    target_ends = {
        "fill": fill_end,
        "promotion": promotion_end,
        "conditional_return": return_end,
        "exit_delay": return_end,
    }
    known_ends = [value for value in target_ends.values() if value is not None]
    return {
        "fill": fill,
        "conditional_net_return": conditional_return,
        "policy_net_return": policy_return,
        "promotion": promoted,
        "exit_delayed": delayed,
        "exit_delay_days": delay_days,
        "target_end_dates": target_ends,
        "label_end_date": max(known_ends) if known_ends else None,
        "is_mature": status in FINAL_TRADE_STATUSES,
    }


def _label_quality(trade: Mapping[str, Any]) -> dict[str, Any]:
    buy = trade.get("buy")
    exit_payload = trade.get("exit")
    validation = trade.get("t_day_validation")
    return {
        "fill": buy.get("label_quality") if isinstance(buy, Mapping) else None,
        "conditional_return": (
            exit_payload.get("label_quality")
            if isinstance(exit_payload, Mapping)
            else None
        ),
        "promotion": (
            "EXACT_UNADJUSTED_DAILY_CLOSE"
            if isinstance(validation, Mapping) and validation.get("status") == "VERIFIED"
            else None
        ),
    }


def _trade_index(state: Mapping[str, Any]) -> dict[tuple[str, int], Mapping[str, Any]]:
    result: dict[tuple[str, int], Mapping[str, Any]] = {}
    trades = state.get("trades", [])
    if not isinstance(trades, list):
        raise ContractError("state.trades must be a list")
    for trade in trades:
        if not isinstance(trade, Mapping):
            raise ContractError("trade must be an object")
        decision_date = normalize_date(trade.get("decision_date"), "trade.decision_date")
        rank = trade.get("rank")
        if isinstance(rank, bool) or not isinstance(rank, int) or rank <= 0:
            raise ContractError("trade.rank must be a positive integer")
        key = (decision_date, rank)
        if key in result:
            raise ContractError(f"duplicate trade identity: {decision_date}:R{rank}")
        result[key] = trade
    return result


def build_training_dataset(state: Mapping[str, Any]) -> dict[str, Any]:
    """Build a deterministic, point-in-time dataset from frozen local state.

    The builder is deliberately pure and performs no market or network reads.
    Unknown labels remain ``None``.  Rows are marked mature only after the
    executable policy outcome is final.
    """

    if not isinstance(state, Mapping):
        raise ContractError("state must be an object")
    trades = _trade_index(state)
    signals = state.get("signals", [])
    if not isinstance(signals, list):
        raise ContractError("state.signals must be a list")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    expected_trade_keys: set[tuple[str, int]] = set()

    for signal in signals:
        if not isinstance(signal, Mapping):
            raise ContractError("signal must be an object")
        decision_date = normalize_date(signal.get("decision_date"), "signal.decision_date")
        buy_date = normalize_date(signal.get("buy_date"), "signal.buy_date")
        planned_exit = normalize_date(signal.get("exit_date"), "signal.exit_date")
        signal_id = str(signal.get("signal_id") or "").strip()
        if not signal_id:
            raise ContractError("signal_id is required")
        provenance = _source_provenance(signal)
        candidates = signal.get("candidates", [])
        if not isinstance(candidates, list):
            raise ContractError("signal.candidates must be a list")
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                raise ContractError("candidate must be an object")
            rank = candidate.get("rank")
            if isinstance(rank, bool) or not isinstance(rank, int) or rank <= 0:
                raise ContractError("candidate.rank must be a positive integer")
            ts_code = normalize_ts_code(candidate.get("ts_code"))
            candidate_id = f"{decision_date}:{ts_code}"
            row_id = f"{signal_id}:{ts_code}"
            if row_id in seen:
                raise ContractError(f"duplicate training row: {row_id}")
            seen.add(row_id)
            key = (decision_date, rank)
            expected_trade_keys.add(key)
            trade = trades.get(key)
            if trade is None:
                raise ContractError(f"missing shadow trade for {decision_date}:R{rank}")
            if (
                str(trade.get("signal_id") or "") != signal_id
                or normalize_ts_code(trade.get("ts_code")) != ts_code
            ):
                raise ContractError(f"shadow trade identity mismatch for {candidate_id}")

            raw_feature_asof = (
                candidate.get("feature_asof")
                if "feature_asof" in candidate
                else signal.get("feature_asof", decision_date)
            )
            feature_asof = normalize_date(raw_feature_asof, "feature_asof")
            if feature_asof > decision_date:
                raise ContractError("feature_asof cannot be later than decision_date")
            labels = _labels(trade)
            if labels["is_mature"] and labels["label_end_date"] is None:
                raise ContractError("mature row requires label_end_date")

            rows.append(
                {
                    "row_id": row_id,
                    "signal_id": signal_id,
                    "candidate_id": candidate_id,
                    "decision_date": decision_date,
                    "buy_date": buy_date,
                    "planned_exit_date": planned_exit,
                    "rank": rank,
                    "ts_code": ts_code,
                    "name": candidate.get("name"),
                    "feature_asof": feature_asof,
                    "feature_version": _feature_version(signal, candidate),
                    "model_version": signal.get("model_version"),
                    "source_provenance": copy.deepcopy(provenance),
                    "source_ranks": copy.deepcopy(candidate.get("source_ranks", {})),
                    "features": copy.deepcopy(candidate.get("features", {})),
                    "labels": labels,
                    "label_quality": _label_quality(trade),
                    "trade_status": trade.get("status"),
                }
            )

    extra_trades = set(trades) - expected_trade_keys
    if extra_trades:
        raise ContractError(f"orphan shadow trades: {sorted(extra_trades)}")
    rows.sort(key=lambda item: (item["decision_date"], item["rank"], item["ts_code"]))
    return {
        "schema_version": TRAINING_DATASET_SCHEMA,
        "row_count": len(rows),
        "mature_count": sum(bool(item["labels"]["is_mature"]) for item in rows),
        "rows": rows,
    }

