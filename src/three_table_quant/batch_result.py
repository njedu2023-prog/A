from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .domain import ContractError


VALIDATION_SCHEDULE_LOCAL_TIME = "19:00"
OUTPUT_SCHEDULE_LOCAL_TIME = "21:30"


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{field_name} must be an object")
    return value


def _timestamp(value: Any, field_name: str) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ContractError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{field_name} must include a timezone")
    return parsed


def _require_fresh(value: Any, started_at: str | None, field_name: str) -> None:
    if started_at is None:
        return
    if _timestamp(value, field_name) < _timestamp(started_at, "started_at"):
        raise ContractError(f"{field_name} predates this batch start")


def _iso_market_date(value: str) -> str:
    raw = str(value or "").strip()
    if len(raw) != 8 or not raw.isdigit():
        raise ContractError("market_date must be YYYYMMDD")
    try:
        return datetime.strptime(raw, "%Y%m%d").date().isoformat()
    except ValueError as exc:
        raise ContractError("market_date must be a valid date") from exc


def validate_batch_result(
    payload: Mapping[str, Any],
    mode: str,
    *,
    target_decision_date: str | None = None,
    market_date: str | None = None,
    started_at: str | None = None,
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
        _require_fresh(
            validation.get("last_attempted_at"),
            started_at,
            "validation.last_attempted_at",
        )
        _require_fresh(
            validation.get("last_completed_at"),
            started_at,
            "validation.last_completed_at",
        )
        if market_date and validation.get("market_date") != _iso_market_date(
            market_date
        ):
            raise ContractError("validation batch does not match its market date")
        for field_name in (
            "trade_count",
            "t_day_verified_count",
            "closed_trade_count",
        ):
            value = validation.get(field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ContractError(f"validation batch has invalid {field_name}")
        obligation_fields = (
            "due",
            "final",
            "pending_data",
            "delayed",
            "failed",
        )
        has_obligation_result = any(
            field_name in validation
            for field_name in ("result_status", *obligation_fields)
        )
        if has_obligation_result:
            if validation.get("result_status") not in {
                "SUCCESS_NO_DUE",
                "SUCCESS",
                "DEGRADED",
            }:
                raise ContractError("validation batch has invalid result_status")
            for field_name in obligation_fields:
                value = validation.get(field_name)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ContractError(
                        f"validation batch has invalid obligation {field_name}"
                    )
            if validation["due"] != sum(
                validation[field_name]
                for field_name in obligation_fields
                if field_name != "due"
            ):
                raise ContractError("validation obligation counts do not reconcile")
            batch_error = validation.get("batch_error")
            expected_status = (
                "DEGRADED"
                if batch_error
                or validation["pending_data"]
                or validation["delayed"]
                or validation["failed"]
                else (
                    "SUCCESS_NO_DUE"
                    if validation["due"] == 0
                    else "SUCCESS"
                )
            )
            if validation["result_status"] != expected_status:
                raise ContractError(
                    "validation result_status disagrees with obligation counts"
                )
        return

    if mode != "output":
        raise ContractError(f"unsupported batch mode: {mode!r}")

    output = _mapping(runs.get("output"), "automation_runs.output")
    if output.get("scheduled_local_time") != OUTPUT_SCHEDULE_LOCAL_TIME:
        raise ContractError("output batch schedule is not 21:30")
    if not output.get("last_attempted_at") or not output.get("status"):
        raise ContractError("output batch is missing its attempt result")
    _require_fresh(
        output.get("last_attempted_at"),
        started_at,
        "output.last_attempted_at",
    )

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
        _require_fresh(
            output.get("last_completed_at"),
            started_at,
            "output.last_completed_at",
        )
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
    parser.add_argument("--market-date")
    parser.add_argument("--started-at")
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
        market_date=args.market_date or None,
        started_at=args.started_at or None,
    )
    print(f"{args.mode} batch result verified: {dashboard_path}")


if __name__ == "__main__":
    main()
