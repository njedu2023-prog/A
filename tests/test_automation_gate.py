from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from three_table_quant.automation_gate import main, should_run_batch
from three_table_quant.domain import ContractError


def dashboard() -> dict:
    return {
        "schema_version": "dashboard_v1",
        "current_run": {
            "status": "NO_CANDIDATE",
            "completed": True,
            "decision_date": "2026-08-10",
            "intersection_count": 0,
        },
        "automation_runs": {
            "validation": {
                "status": "COMPLETED",
                "market_date": "2026-08-10",
            },
            "output": {
                "status": "COMPLETED",
                "last_completed_at": "2026-08-10T21:30:05+08:00",
            },
        },
    }


class AutomationGateTests(unittest.TestCase):
    def test_completed_validation_for_same_date_is_idempotent(self) -> None:
        self.assertFalse(
            should_run_batch(dashboard(), "validation", "20260810")
        )

        successful = dashboard()
        successful["automation_runs"]["validation"]["result_status"] = "SUCCESS"
        self.assertFalse(
            should_run_batch(successful, "validation", "20260810")
        )

        no_due = dashboard()
        no_due["automation_runs"]["validation"][
            "result_status"
        ] = "SUCCESS_NO_DUE"
        self.assertFalse(should_run_batch(no_due, "validation", "20260810"))

    def test_degraded_validation_remains_retryable(self) -> None:
        payload = dashboard()
        payload["automation_runs"]["validation"].update(
            {
                "result_status": "DEGRADED",
                "pending_data": 1,
                "delayed": 0,
                "failed": 0,
            }
        )
        self.assertTrue(should_run_batch(payload, "validation", "20260810"))

    def test_validation_with_stale_or_incomplete_record_runs(self) -> None:
        payload = dashboard()
        payload["automation_runs"]["validation"]["market_date"] = "2026-08-07"
        self.assertTrue(should_run_batch(payload, "validation", "20260810"))
        payload["automation_runs"]["validation"]["status"] = "DEGRADED"
        self.assertTrue(should_run_batch(payload, "validation", "20260810"))

    def test_completed_zero_candidate_output_is_idempotent(self) -> None:
        self.assertFalse(should_run_batch(dashboard(), "output", "20260810"))

    def test_output_requires_both_current_date_and_completed_automation(self) -> None:
        stale = dashboard()
        stale["current_run"]["decision_date"] = "2026-08-07"
        self.assertTrue(should_run_batch(stale, "output", "20260810"))

        incomplete = dashboard()
        incomplete["automation_runs"]["output"]["status"] = "INPUT_BLOCKED"
        self.assertTrue(should_run_batch(incomplete, "output", "20260810"))

        not_frozen = dashboard()
        not_frozen["current_run"]["completed"] = False
        self.assertTrue(should_run_batch(not_frozen, "output", "20260810"))

    def test_missing_legacy_records_remain_runnable(self) -> None:
        self.assertTrue(
            should_run_batch({"schema_version": "dashboard_v1"}, "output", "20260810")
        )
        self.assertTrue(
            should_run_batch(
                {"schema_version": "dashboard_v1"},
                "validation",
                "20260810",
            )
        )

    def test_force_overrides_existing_completion(self) -> None:
        self.assertTrue(
            should_run_batch(
                dashboard(),
                "validation",
                "20260810",
                force=True,
            )
        )
        self.assertTrue(
            should_run_batch(
                dashboard(),
                "output",
                "20260810",
                force=True,
            )
        )

    def test_malformed_dates_and_completion_types_fail_closed(self) -> None:
        with self.assertRaisesRegex(ContractError, "valid YYYYMMDD"):
            should_run_batch(dashboard(), "output", "20260230")

        invalid = dashboard()
        invalid["current_run"]["completed"] = "true"
        with self.assertRaisesRegex(ContractError, "must be boolean"):
            should_run_batch(invalid, "output", "20260810")

        invalid = dashboard()
        invalid["automation_runs"]["validation"]["market_date"] = "unknown"
        with self.assertRaisesRegex(ContractError, "calendar date"):
            should_run_batch(invalid, "validation", "20260810")

    def test_cli_prints_github_output_style_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dashboard.json"
            path.write_text(json.dumps(dashboard()), encoding="utf-8")
            stdout = StringIO()
            argv = [
                "automation_gate",
                "--mode",
                "output",
                "--dashboard",
                str(path),
                "--market-date",
                "20260810",
            ]
            with patch.object(sys, "argv", argv), redirect_stdout(stdout):
                main()
        self.assertEqual(stdout.getvalue(), "should_run=false\n")


if __name__ == "__main__":
    unittest.main()
