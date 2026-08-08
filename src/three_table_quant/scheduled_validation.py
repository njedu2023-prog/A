from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .dashboard import build_dashboard, validate_dashboard
from .http import HttpClient
from .ledger import load_json, load_state, save_json, settle_trades
from .market import ResilientMarketData
from .pipeline import (
    _ensure_all_candidate_shadow_ledger,
    _now,
    _refresh_model_registry,
    load_config,
)


VALIDATION_SCHEDULE_LOCAL_TIME = "15:20"
OUTPUT_SCHEDULE_LOCAL_TIME = "21:30"


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
    iso_decision_date = (
        f"{decision_date[:4]}-{decision_date[4:6]}-{decision_date[6:]}"
    )
    return {
        "status": signal["status"],
        "message": f"D日信号已冻结；保留{len(signal.get('candidates', []))}支候选且不事后改写",
        "completed": True,
        "completed_at": str(signal.get("generated_at") or _now()),
        "decision_date": iso_decision_date,
        "outcome": "COMPLETED_FROZEN_SIGNAL",
        "source_table_counts": {},
        "source_dates": {},
        "intersection_count": len(signal.get("candidates", [])),
        "ranking_engine": signal.get("ranking_engine") or {},
    }


def _automation_runs(
    previous_dashboard: dict[str, Any],
    *,
    completed_at: str,
    state: dict[str, Any],
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
    runs["output"] = output
    runs["validation"] = {
        "scheduled_local_time": VALIDATION_SCHEDULE_LOCAL_TIME,
        "last_attempted_at": completed_at,
        "last_completed_at": completed_at,
        "status": "COMPLETED",
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
    settle_trades(state, truth, market, config["execution"])
    registry, _, _, registry_issue = _refresh_model_registry(
        state,
        config,
        config_path,
    )

    generated_at = _now()
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
