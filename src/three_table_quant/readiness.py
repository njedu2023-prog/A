from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from typing import Any

from .pipeline import run_pipeline


RETRYABLE_SOURCE_CODES = frozenset(
    {
        "SOURCE_HEAD_FAILED",
        "SOURCE_LOAD_FAILED",
        "SOURCE_SET_INCOMPLETE",
        "SOURCE_DATE_MISMATCH",
        "BUY_DATE_MISMATCH",
        "EXIT_DATE_MISMATCH",
        "SOURCE_TARGET_DATE_NOT_READY",
    }
)
COMPLETED_STATUSES = frozenset({"RANKED", "NO_CANDIDATE"})


def _error_codes(dashboard: dict[str, Any]) -> set[str]:
    return {
        str(issue.get("code") or "")
        for issue in dashboard.get("source_issues", [])
        if isinstance(issue, dict) and issue.get("severity") == "error"
    }


def _summary(dashboard: dict[str, Any], attempt: int) -> dict[str, Any]:
    current = dashboard.get("current_run") or {}
    return {
        "attempt": attempt,
        "status": current.get("status"),
        "completed": current.get("completed"),
        "decision_date": current.get("decision_date"),
        "intersection_count": current.get("intersection_count"),
        "generated_at": dashboard.get("generated_at"),
        "error_codes": sorted(_error_codes(dashboard)),
    }


def run_when_sources_ready(
    config_path: str,
    *,
    attempts: int,
    interval_seconds: float,
    runner: Callable[[str], dict[str, Any]] = run_pipeline,
    sleeper: Callable[[float], None] = time.sleep,
    emit: Callable[[str], None] = print,
    target_decision_date: str | None = None,
) -> dict[str, Any]:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    if interval_seconds < 0:
        raise ValueError("interval_seconds must be nonnegative")

    last_dashboard: dict[str, Any] | None = None
    for attempt in range(1, attempts + 1):
        dashboard = (
            runner(
                config_path,
                target_decision_date=target_decision_date,
            )
            if target_decision_date
            else runner(config_path)
        )
        last_dashboard = dashboard
        current = dashboard.get("current_run") or {}
        status = str(current.get("status") or "")
        completed = current.get("completed") is True
        summary = _summary(dashboard, attempt)
        emit(json.dumps(summary, ensure_ascii=False, sort_keys=True))

        if completed and status in COMPLETED_STATUSES:
            return dashboard

        error_codes = _error_codes(dashboard)
        retryable = (
            status == "INPUT_BLOCKED"
            and bool(error_codes)
            and error_codes <= RETRYABLE_SOURCE_CODES
        )
        if not retryable:
            raise RuntimeError(
                "shadow pipeline did not complete and is not waiting on "
                f"retryable source readiness: {json.dumps(summary, ensure_ascii=False)}"
            )
        if attempt < attempts:
            emit(
                f"三源尚未同日就绪；{interval_seconds:g}秒后进行"
                f"第{attempt + 1}/{attempts}次检查"
            )
            sleeper(interval_seconds)

    if last_dashboard is None:  # defensive; attempts validation makes this unreachable
        raise RuntimeError("source readiness loop produced no dashboard")
    emit(
        "三源在等待窗口内仍未同日就绪；保留并发布最新INPUT_BLOCKED状态，"
        "不伪造0支名单"
    )
    return last_dashboard


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Wait for aligned three-source inputs, then run the shadow pipeline"
    )
    parser.add_argument("--config", default="config/system.json")
    parser.add_argument("--attempts", type=int, default=25)
    parser.add_argument("--interval-seconds", type=float, default=300.0)
    parser.add_argument("--target-decision-date")
    args = parser.parse_args()
    run_when_sources_ready(
        args.config,
        attempts=args.attempts,
        interval_seconds=args.interval_seconds,
        target_decision_date=args.target_decision_date or None,
    )


if __name__ == "__main__":
    main()
