from __future__ import annotations

import unittest

from three_table_quant.readiness import run_when_sources_ready


def dashboard(
    status: str,
    *,
    completed: bool,
    error_codes: tuple[str, ...] = (),
    intersection_count: int | None = None,
) -> dict:
    return {
        "generated_at": "2026-08-07T21:30:00+08:00",
        "current_run": {
            "status": status,
            "completed": completed,
            "decision_date": "2026-08-07" if completed else None,
            "intersection_count": intersection_count,
        },
        "source_issues": [
            {"code": code, "severity": "error"}
            for code in error_codes
        ],
    }


class SourceReadinessTests(unittest.TestCase):
    def test_retries_source_lag_until_ranked(self) -> None:
        results = iter(
            [
                dashboard(
                    "INPUT_BLOCKED",
                    completed=False,
                    error_codes=("SOURCE_DATE_MISMATCH",),
                ),
                dashboard(
                    "RANKED",
                    completed=True,
                    intersection_count=4,
                ),
            ]
        )
        sleeps: list[float] = []
        emitted: list[str] = []

        result = run_when_sources_ready(
            "config/system.json",
            attempts=3,
            interval_seconds=300,
            runner=lambda _: next(results),
            sleeper=sleeps.append,
            emit=emitted.append,
        )

        self.assertEqual(result["current_run"]["intersection_count"], 4)
        self.assertEqual(sleeps, [300])
        self.assertTrue(any("第2/3次检查" in item for item in emitted))

    def test_zero_intersection_is_a_completed_output(self) -> None:
        result = run_when_sources_ready(
            "config/system.json",
            attempts=1,
            interval_seconds=300,
            runner=lambda _: dashboard(
                "NO_CANDIDATE",
                completed=True,
                intersection_count=0,
            ),
            sleeper=lambda _: self.fail("completed zero result must not sleep"),
            emit=lambda _: None,
        )
        self.assertEqual(result["current_run"]["intersection_count"], 0)

    def test_target_date_lag_is_retried_and_forwarded_to_pipeline(self) -> None:
        calls: list[tuple[str, str | None]] = []
        results = iter(
            [
                dashboard(
                    "INPUT_BLOCKED",
                    completed=False,
                    error_codes=("SOURCE_TARGET_DATE_NOT_READY",),
                ),
                dashboard("RANKED", completed=True, intersection_count=3),
            ]
        )

        def runner(config: str, *, target_decision_date: str | None = None) -> dict:
            calls.append((config, target_decision_date))
            return next(results)

        result = run_when_sources_ready(
            "config/system.json",
            attempts=2,
            interval_seconds=0,
            target_decision_date="20260810",
            runner=runner,
            sleeper=lambda _: None,
            emit=lambda _: None,
        )

        self.assertEqual(result["current_run"]["intersection_count"], 3)
        self.assertEqual(
            calls,
            [
                ("config/system.json", "20260810"),
                ("config/system.json", "20260810"),
            ],
        )

    def test_strict_intersection_contract_block_retries_without_faking_zero(self) -> None:
        blocked = dashboard(
            "INPUT_BLOCKED",
            completed=False,
            error_codes=("STRICT_INTERSECTION_CONTRACT_FAILED",),
            intersection_count=None,
        )
        ranked = dashboard("RANKED", completed=True, intersection_count=2)
        results = iter((blocked, ranked))
        sleeps: list[float] = []

        result = run_when_sources_ready(
            "config/system.json",
            attempts=2,
            interval_seconds=1,
            runner=lambda _: next(results),
            sleeper=sleeps.append,
            emit=lambda _: None,
        )

        self.assertIsNone(blocked["current_run"]["intersection_count"])
        self.assertEqual(result["current_run"]["intersection_count"], 2)
        self.assertEqual(sleeps, [1])

    def test_timeout_returns_latest_blocked_dashboard_for_publication(self) -> None:
        blocked = dashboard(
            "INPUT_BLOCKED",
            completed=False,
            error_codes=("SOURCE_LOAD_FAILED",),
        )
        sleeps: list[float] = []
        result = run_when_sources_ready(
            "config/system.json",
            attempts=2,
            interval_seconds=1,
            runner=lambda _: blocked,
            sleeper=sleeps.append,
            emit=lambda _: None,
        )
        self.assertIs(result, blocked)
        self.assertEqual(sleeps, [1])

    def test_nonretryable_contract_failure_is_not_hidden(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "not waiting on retryable"):
            run_when_sources_ready(
                "config/system.json",
                attempts=3,
                interval_seconds=0,
                runner=lambda _: dashboard(
                    "INPUT_BLOCKED",
                    completed=False,
                    error_codes=("TRADING_CALENDAR_MISMATCH",),
                ),
                sleeper=lambda _: self.fail("nonretryable failure must not sleep"),
                emit=lambda _: None,
            )


if __name__ == "__main__":
    unittest.main()
