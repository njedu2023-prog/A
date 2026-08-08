from __future__ import annotations

import argparse
import copy
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .dashboard import build_dashboard, validate_dashboard
from .domain import ContractError, normalize_date
from .http import HttpClient
from .ledger import load_json, load_state, save_json, settle_trades
from .market import ResilientMarketData
from .pipeline import (
    _ensure_all_candidate_shadow_ledger,
    _now,
    _refresh_model_registry,
    load_config,
)


VALIDATION_SCHEDULE_LOCAL_TIME = "19:00"
OUTPUT_SCHEDULE_LOCAL_TIME = "21:30"


def _validation_clock(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ContractError("validation execution time must be ISO-8601") from exc
    zone = ZoneInfo("Asia/Shanghai")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def _date_due(value: Any, today: str, field_name: str) -> bool:
    if value is None or not str(value).strip():
        return False
    return normalize_date(value, field_name) <= today


def _t_day_due(
    trade: dict[str, Any],
    now: datetime,
    execution: dict[str, Any],
) -> bool:
    validation = trade.get("t_day_validation")
    if not isinstance(validation, dict) or validation.get("status") == "VERIFIED":
        return False
    trade_date = validation.get("trade_date") or trade.get("buy_date")
    if not trade_date:
        return False
    day = normalize_date(trade_date, "t_day_validation.trade_date")
    today = now.strftime("%Y%m%d")
    if day < today:
        return True
    if day > today:
        return False
    threshold = str(execution.get("t_validation_after_local_time", "15:10"))
    try:
        gate = datetime.strptime(threshold, "%H:%M").time()
    except ValueError as exc:
        raise ContractError("t_validation_after_local_time must use HH:MM") from exc
    return now.time().replace(tzinfo=None) >= gate


def _due_obligations(
    state: dict[str, Any],
    now: datetime,
    execution: dict[str, Any],
) -> list[dict[str, str]]:
    """Snapshot every unfinished validation duty that is due in this batch."""

    today = now.strftime("%Y%m%d")
    obligations: list[dict[str, str]] = []
    for trade in state.get("trades", []):
        trade_id = str(trade.get("trade_id") or "")
        if not trade_id:
            raise ContractError("validation obligation requires trade_id")
        status = str(trade.get("status") or "")
        if _t_day_due(trade, now, execution):
            obligations.append({"trade_id": trade_id, "kind": "T_DAY"})
        if status in {"PENDING_BUY", "BUY_UNVERIFIABLE"} and _date_due(
            trade.get("buy_date"),
            today,
            "buy_date",
        ):
            obligations.append({"trade_id": trade_id, "kind": "ENTRY"})
        if status not in {"CLOSED", "CASH", "BUY_UNFILLED"} and _date_due(
            trade.get("planned_exit_date"),
            today,
            "planned_exit_date",
        ):
            # If entry truth is still pending, the expired exit duty remains
            # visible.  BUY_UNFILLED later resolves it as a final no-exit duty.
            obligations.append({"trade_id": trade_id, "kind": "EXIT"})
    return obligations


def _obligation_outcome(kind: str, trade: dict[str, Any] | None) -> str:
    if trade is None:
        return "failed"
    status = str(trade.get("status") or "")
    if kind == "T_DAY":
        validation = trade.get("t_day_validation")
        validation_status = (
            str(validation.get("status") or "")
            if isinstance(validation, dict)
            else ""
        )
        if validation_status == "VERIFIED":
            return "final"
        if validation_status in {"PENDING", "UNVERIFIABLE"}:
            return "pending_data"
        return "failed"
    if kind == "ENTRY":
        if status == "BUY_UNFILLED" or (
            isinstance(trade.get("buy"), dict)
            and status not in {"PENDING_BUY", "BUY_UNVERIFIABLE"}
        ):
            return "final"
        if status in {"PENDING_BUY", "BUY_UNVERIFIABLE"}:
            return "pending_data"
        return "failed"
    if kind == "EXIT":
        if status in {"CLOSED", "CASH", "BUY_UNFILLED"}:
            return "final"
        if status == "EXIT_DELAYED":
            return "delayed"
        if status in {
            "PENDING_BUY",
            "BUY_UNVERIFIABLE",
            "OPEN",
            "EXIT_UNVERIFIABLE",
        }:
            return "pending_data"
        return "failed"
    return "failed"


def _validation_summary(
    obligations: list[dict[str, str]],
    state: dict[str, Any],
    *,
    batch_error: str | None = None,
) -> dict[str, Any]:
    counts = {
        "due": len(obligations),
        "final": 0,
        "pending_data": 0,
        "delayed": 0,
        "failed": 0,
    }
    if batch_error is not None:
        counts["failed"] = counts["due"]
    else:
        trades = {
            str(trade.get("trade_id") or ""): trade
            for trade in state.get("trades", [])
        }
        for obligation in obligations:
            outcome = _obligation_outcome(
                obligation["kind"],
                trades.get(obligation["trade_id"]),
            )
            counts[outcome] += 1
    if (
        batch_error is not None
        or counts["pending_data"]
        or counts["delayed"]
        or counts["failed"]
    ):
        result_status = "DEGRADED"
    elif counts["due"] == 0:
        result_status = "SUCCESS_NO_DUE"
    else:
        result_status = "SUCCESS"
    return {
        **counts,
        "result_status": result_status,
        "batch_error": batch_error,
    }


def _settle_validation_batch(
    state: dict[str, Any],
    truth: dict[str, Any],
    market: Any,
    execution: dict[str, Any],
    executed_at: str,
) -> dict[str, Any]:
    now = _validation_clock(executed_at)
    snapshot = copy.deepcopy(state)
    obligations: list[dict[str, str]] = []
    try:
        obligations = _due_obligations(state, now, execution)
        settle_trades(
            state,
            truth,
            market,
            execution,
            asof_at=now,
        )
    except Exception as exc:
        state.clear()
        state.update(snapshot)
        return _validation_summary(
            obligations,
            state,
            batch_error=f"{type(exc).__name__}: {exc}",
        )
    return _validation_summary(obligations, state)


def _fallback_current_run(
    previous_dashboard: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    previous = previous_dashboard.get("current_run")
    if isinstance(previous, dict):
        return dict(previous)

    signals = sorted(
        state.get("signals", []),
        key=lambda item: str(item.get("decision_date") or ""),
    )
    if not signals:
        return {
            "status": "INPUT_BLOCKED",
            "message": "尚无已冻结名单；验证批次已执行",
            "completed": False,
            "completed_at": None,
            "decision_date": None,
            "outcome": "NO_FROZEN_SIGNAL",
            "source_table_counts": {},
            "source_dates": {},
            "intersection_count": None,
        }

    signal = signals[-1]
    decision_date = str(signal["decision_date"])
    candidate_count = len(signal.get("candidates", []))
    iso_decision_date = (
        f"{decision_date[:4]}-{decision_date[4:6]}-{decision_date[6:]}"
    )
    return {
        "status": signal["status"],
        "message": f"D日信号已冻结；保留{candidate_count}支候选且不事后改写",
        "completed": True,
        "completed_at": str(signal.get("generated_at") or _now()),
        "decision_date": iso_decision_date,
        "outcome": (
            "COMPLETED_ZERO_INTERSECTION"
            if candidate_count == 0
            else "COMPLETED_FROZEN_SIGNAL"
        ),
        "source_table_counts": {},
        "source_dates": {},
        "intersection_count": candidate_count,
        "ranking_engine": signal.get("ranking_engine") or {},
    }


def _automation_runs(
    previous_dashboard: dict[str, Any],
    *,
    completed_at: str,
    state: dict[str, Any],
    validation_summary: dict[str, Any],
) -> dict[str, Any]:
    previous = previous_dashboard.get("automation_runs")
    runs = dict(previous) if isinstance(previous, dict) else {}
    current = previous_dashboard.get("current_run") or {}
    output = runs.get("output")
    if not isinstance(output, dict):
        prior_output_at = (
            current.get("completed_at")
            if current.get("completed") is True
            else None
        )
        output = {
            "scheduled_local_time": OUTPUT_SCHEDULE_LOCAL_TIME,
            "last_attempted_at": prior_output_at,
            "last_completed_at": prior_output_at,
            "status": "COMPLETED" if prior_output_at else "PENDING_FIRST_RUN",
        }
    else:
        output = dict(output)
        output["scheduled_local_time"] = OUTPUT_SCHEDULE_LOCAL_TIME
    runs["output"] = output
    runs["validation"] = {
        "scheduled_local_time": VALIDATION_SCHEDULE_LOCAL_TIME,
        "market_date": _validation_clock(completed_at).date().isoformat(),
        "last_attempted_at": completed_at,
        "last_completed_at": completed_at,
        "status": "COMPLETED",
        **validation_summary,
        "trade_count": len(state.get("trades", [])),
        "t_day_verified_count": sum(
            (trade.get("t_day_validation") or {}).get("status") == "VERIFIED"
            for trade in state.get("trades", [])
        ),
        "closed_trade_count": sum(
            trade.get("status") in {"CLOSED", "CASH", "BUY_UNFILLED"}
            for trade in state.get("trades", [])
        ),
    }
    return runs


def run_scheduled_validation(
    config_path: str | Path = "config/system.json",
) -> dict[str, Any]:
    """Settle every due shadow record without loading or ranking today's sources."""

    config = load_config(config_path)
    paths = config["paths"]
    state = load_state(paths["state"])
    truth = load_json(
        paths["execution_truth"],
        {"schema_version": "execution_truth_v1", "auctions": {}},
    )
    previous_dashboard = load_json(paths["dashboard"], {})
    issue_payload = load_json(
        paths["source_issues"],
        {"schema_version": "source_issues_v1", "issues": []},
    )
    market = ResilientMarketData(HttpClient())

    _ensure_all_candidate_shadow_ledger(state, list(config["tracked_ranks"]))
    generated_at = _now()
    validation_summary = _settle_validation_batch(
        state,
        truth,
        market,
        config["execution"],
        generated_at,
    )
    registry, _, _, registry_issue = _refresh_model_registry(
        state,
        config,
        config_path,
    )

    current_run = _fallback_current_run(previous_dashboard, state)
    issues = list(issue_payload.get("issues") or [])
    if registry_issue is not None:
        issues.append(registry_issue.to_dict())
    dashboard = build_dashboard(
        state,
        issues,
        generated_at,
        current_run,
        list(config["tracked_ranks"]),
    )
    dashboard["automation_runs"] = _automation_runs(
        previous_dashboard,
        completed_at=generated_at,
        state=state,
        validation_summary=validation_summary,
    )
    validate_dashboard(dashboard)
    save_json(paths["state"], state)
    save_json(paths["dashboard"], dashboard)
    save_json(paths["model_registry"], registry)
    return dashboard


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate due shadow records without waiting for three-source output"
    )
    parser.add_argument("--config", default="config/system.json")
    args = parser.parse_args()
    run_scheduled_validation(args.config)


if __name__ == "__main__":
    main()
