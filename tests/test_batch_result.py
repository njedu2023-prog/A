from __future__ import annotations

import copy
import unittest

from three_table_quant.batch_result import validate_batch_result
from three_table_quant.domain import ContractError


def payload() -> dict:
    return {
        "schema_version": "dashboard_v1",
        "current_run": {
            "status": "RANKED",
            "completed": True,
            "decision_date": "2026-08-10",
            "target_decision_date": "2026-08-10",
            "intersection_count": 3,
        },
        "automation_runs": {
            "validation": {
                "scheduled_local_time": "19:00",
                "market_date": "2026-08-10",
                "asof_at": "2026-08-10T19:00:08+08:00",
                "last_attempted_at": "2026-08-10T19:00:08+08:00",
                "last_completed_at": "2026-08-10T19:00:08+08:00",
                "status": "COMPLETED",
                "result_status": "DEGRADED",
                "due": 4,
                "final": 3,
                "pending_data": 0,
                "delayed": 1,
                "failed": 0,
                "batch_error": None,
                "trade_count": 4,
                "t_day_verified_count": 4,
                "closed_trade_count": 1,
            },
            "output": {
                "scheduled_local_time": "21:30",
                "last_attempted_at": "2026-08-10T21:36:00+08:00",
                "last_completed_at": "2026-08-10T21:36:00+08:00",
                "status": "COMPLETED",
            },
        },
    }


class BatchResultTests(unittest.TestCase):
    def test_validation_result_is_publishable(self) -> None:
        validate_batch_result(
            payload(),
            "validation",
            market_date="20260810",
            started_at="2026-08-10T10:59:59Z",
        )

    def test_output_result_matches_target_trading_date(self) -> None:
        validate_batch_result(
            payload(),
            "output",
            target_decision_date="20260810",
            started_at="2026-08-10T13:35:00Z",
        )

    def test_stale_batch_cannot_reuse_a_previous_success(self) -> None:
        with self.assertRaisesRegex(ContractError, "predates"):
            validate_batch_result(
                payload(),
                "validation",
                started_at="2026-08-10T19:01:00+08:00",
            )
        with self.assertRaisesRegex(ContractError, "predates"):
            validate_batch_result(
                payload(),
                "output",
                started_at="2026-08-10T21:37:00+08:00",
            )

    def test_validation_market_date_must_match(self) -> None:
        with self.assertRaisesRegex(ContractError, "market date"):
            validate_batch_result(
                payload(),
                "validation",
                market_date="20260811",
            )

    def test_validation_asof_must_match_market_date(self) -> None:
        invalid = copy.deepcopy(payload())
        invalid["automation_runs"]["validation"]["asof_at"] = (
            "2026-08-09T19:00:00+08:00"
        )
        with self.assertRaisesRegex(ContractError, "asof_at"):
            validate_batch_result(invalid, "validation")

    def test_stale_output_is_rejected(self) -> None:
        stale = copy.deepcopy(payload())
        stale["current_run"]["decision_date"] = "2026-08-07"
        with self.assertRaisesRegex(ContractError, "does not match"):
            validate_batch_result(
                stale,
                "output",
                target_decision_date="20260810",
            )

    def test_validation_missing_actual_time_is_rejected(self) -> None:
        invalid = copy.deepcopy(payload())
        invalid["automation_runs"]["validation"]["last_completed_at"] = None
        with self.assertRaisesRegex(ContractError, "actual execution time"):
            validate_batch_result(invalid, "validation")

    def test_validation_no_due_is_a_successful_executed_batch(self) -> None:
        no_due = copy.deepcopy(payload())
        validation = no_due["automation_runs"]["validation"]
        validation.update(
            {
                "result_status": "SUCCESS_NO_DUE",
                "due": 0,
                "final": 0,
                "pending_data": 0,
                "delayed": 0,
                "failed": 0,
            }
        )
        validate_batch_result(no_due, "validation")

    def test_validation_degraded_counts_must_reconcile(self) -> None:
        degraded = copy.deepcopy(payload())
        validation = degraded["automation_runs"]["validation"]
        validation.update(
            {
                "result_status": "DEGRADED",
                "due": 3,
                "final": 1,
                "pending_data": 1,
                "delayed": 1,
                "failed": 0,
            }
        )
        validate_batch_result(degraded, "validation")

        validation["due"] = 4
        with self.assertRaisesRegex(ContractError, "do not reconcile"):
            validate_batch_result(degraded, "validation")

    def test_validation_delayed_only_must_be_degraded(self) -> None:
        delayed = copy.deepcopy(payload())
        validation = delayed["automation_runs"]["validation"]
        validation.update(
            {
                "result_status": "SUCCESS",
                "due": 1,
                "final": 0,
                "pending_data": 0,
                "delayed": 1,
                "failed": 0,
            }
        )
        with self.assertRaisesRegex(ContractError, "disagrees"):
            validate_batch_result(delayed, "validation")

        validation["result_status"] = "DEGRADED"
        validate_batch_result(delayed, "validation")

    def test_legacy_validation_result_remains_compatible(self) -> None:
        legacy = copy.deepcopy(payload())
        validation = legacy["automation_runs"]["validation"]
        for field_name in (
            "result_status",
            "due",
            "final",
            "pending_data",
            "delayed",
            "failed",
            "batch_error",
        ):
            validation.pop(field_name, None)
        validate_batch_result(legacy, "validation")


if __name__ == "__main__":
    unittest.main()
