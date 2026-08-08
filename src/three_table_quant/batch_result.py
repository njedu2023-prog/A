from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from .domain import ContractError


VALIDATION_SCHEDULE_LOCAL_TIME = "19:00"
OUTPUT_SCHEDULE_LOCAL_TIME = "21:30"


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{field_name} must be an object")
    return value


def validate_batch_result(
    payload: Mapping[str, Any],
    mode: str,
    *,
    target_decision_date: str | None = None,
) -> None:
    """Fail closed unless the selected batch produced a publishable dashboard."""

    if payload.get("schema_version") != "dashboard_v1":
        raise ContractError("batch result must use dashboard_v1")
    runs = _mapping(payload.get("automation_runs"), "automation_runs")
    current_run = _mapping(payload.get("current_run"), "current_run")

    if mode == "validation":
        validation = _mapping(runs.get("validation"), "automation_runs.validation")
        if validation.get("scheduled_local_time") != VALIDATION_SCHEDULE_LOCAL_TIME:
            raise ContractError("validation batch schedule is not 19:00")
        if validation.get("status") != "COMPLETED":
            raise ContractError("validation batch did not complete")
        if not validation.get("last_attempted_at") or not validation.get(
            "last_completed_at"
        ):
            raise ContractError("validation batch is missing actual execution time")
        for field_name in (
            "trade_count",
            "t_day_verified_count",
            "closed_trade_count",
        ):
            value = validation.get(field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ContractError(f"validation batch has invalid {field_name}")
        return

    if mode != "output":
        raise ContractError(f"unsupported batch mode: {mode!r}")

    output = _mapping(runs.get("output"), "automation_runs.output")
    if output.get("scheduled_local_time") != OUTPUT_SCHEDULE_LOCAL_TIME:
        raise ContractError("output batch schedule is not 21:30")
    if not output.get("last_attempted_at") or not output.get("status"):
        raise ContractError("output batch is missing its attempt result")

    completed = current_run.get("completed") is True
    if target_decision_date:
        raw_target = str(target_decision_date)
        if len(raw_target) != 8 or not raw_target.isdigit():
            raise ContractError("target_decision_date must be YYYYMMDD")
        iso_target = f"{raw_target[:4]}-{raw_target[4:6]}-{raw_target[6:]}"
        if current_run.get("target_decision_date") != iso_target:
            raise ContractError("output batch did not retain its target trading date")
        if completed and current_run.get("decision_date") != iso_target:
            raise ContractError("completed output does not match its target trading date")
    if completed:
        if output.get("status") != "COMPLETED" or not output.get(
            "last_completed_at"
        ):
            raise ContractError("completed output batch is missing completion truth")
        intersection_count = current_run.get("intersection_count")
        if (
            not isinstance(intersection_count, int)
            or isinstance(intersection_count, bool)
            or intersection_count < 0
        ):
            raise ContractError("completed output batch has invalid intersection_count")
    elif output.get("status") != current_run.get("status"):
        raise ContractError("blocked output status disagrees with current_run")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify that an automated batch produced a publishable result"
    )
    parser.add_argument("--mode", choices=("validation", "output"), required=True)
    parser.add_argument("--dashboard", default="data/dashboard.v1.json")
    parser.add_argument("--target-decision-date")
    args = parser.parse_args()
    dashboard_path = Path(args.dashboard)
    with dashboard_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ContractError("dashboard payload must be an object")
    validate_batch_result(
        payload,
        args.mode,
        target_decision_date=args.target_decision_date or None,
    )
    print(f"{args.mode} batch result verified: {dashboard_path}")


if __name__ == "__main__":
    main()
