from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .domain import ContractError


AUTOMATION_MODES = frozenset({"validation", "output"})


def _market_date(value: Any, field_name: str = "market_date") -> str:
    raw = str(value or "").strip()
    if len(raw) != 8 or not raw.isdigit():
        raise ContractError(f"{field_name} must be YYYYMMDD")
    try:
        parsed = datetime.strptime(raw, "%Y%m%d")
    except ValueError as exc:
        raise ContractError(f"{field_name} must be a valid YYYYMMDD date") from exc
    return parsed.strftime("%Y%m%d")


def _dashboard_date(value: Any, field_name: str) -> str:
    raw = str(value or "").strip()
    digits = "".join(character for character in raw if character.isdigit())
    if len(digits) != 8:
        raise ContractError(f"{field_name} must identify one calendar date")
    return _market_date(digits, field_name)


def _optional_mapping(
    payload: Mapping[str, Any],
    key: str,
    field_name: str,
) -> Mapping[str, Any]:
    value = payload.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ContractError(f"{field_name} must be an object")
    return value


def should_run_batch(
    payload: Mapping[str, Any],
    mode: str,
    market_date: str,
    *,
    force: bool = False,
) -> bool:
    """Return whether an automated batch still owes work for ``market_date``.

    The gate is deliberately narrow: it only suppresses a run when the
    dashboard carries completion truth for the requested batch and date.
    Missing or older completion records therefore remain runnable.  ``force``
    is an explicit operator override and never changes the dashboard.
    """

    if not isinstance(payload, Mapping):
        raise ContractError("dashboard payload must be an object")
    normalized_date = _market_date(market_date)
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in AUTOMATION_MODES:
        raise ContractError(f"unsupported automation mode: {mode!r}")
    schema_version = payload.get("schema_version")
    if schema_version not in (None, "dashboard_v1"):
        raise ContractError("automation gate requires dashboard_v1")
    if force:
        return True

    automation_runs = _optional_mapping(
        payload,
        "automation_runs",
        "automation_runs",
    )
    if normalized_mode == "validation":
        validation = _optional_mapping(
            automation_runs,
            "validation",
            "automation_runs.validation",
        )
        if validation.get("status") != "COMPLETED":
            return True
        result_status = validation.get("result_status")
        if result_status is not None and result_status not in {
            "SUCCESS",
            "SUCCESS_NO_DUE",
        }:
            # A completed invocation may still have unresolved data, delayed
            # obligations, or failures.  A redundant trigger must be allowed
            # to retry those degraded outcomes.
            return True
        recorded_date = _dashboard_date(
            validation.get("market_date"),
            "automation_runs.validation.market_date",
        )
        return recorded_date != normalized_date

    current_run = _optional_mapping(payload, "current_run", "current_run")
    completed = current_run.get("completed")
    if completed is not None and not isinstance(completed, bool):
        raise ContractError("current_run.completed must be boolean")
    if completed is not True:
        return True
    decision_date = _dashboard_date(
        current_run.get("decision_date"),
        "current_run.decision_date",
    )
    output = _optional_mapping(
        automation_runs,
        "output",
        "automation_runs.output",
    )
    return not (
        decision_date == normalized_date and output.get("status") == "COMPLETED"
    )


def load_dashboard(path: str | Path) -> Mapping[str, Any]:
    dashboard_path = Path(path)
    try:
        with dashboard_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load dashboard {dashboard_path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ContractError("dashboard payload must be an object")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Suppress an already-completed validation or output batch"
    )
    parser.add_argument("--mode", choices=sorted(AUTOMATION_MODES), required=True)
    parser.add_argument("--dashboard", required=True)
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    should_run = should_run_batch(
        load_dashboard(args.dashboard),
        args.mode,
        args.market_date,
        force=args.force,
    )
    print(f"should_run={'true' if should_run else 'false'}")


if __name__ == "__main__":
    main()
