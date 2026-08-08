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
                "last_attempted_at": "2026-08-10T19:00:08+08:00",
                "last_completed_at": "2026-08-10T19:00:08+08:00",
                "status": "COMPLETED",
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
        validate_batch_result(payload(), "validation")

    def test_output_result_matches_target_trading_date(self) -> None:
        validate_batch_result(
            payload(),
            "output",
            target_decision_date="20260810",
        )

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


if __name__ == "__main__":
    unittest.main()
