from __future__ import annotations

import unittest
from unittest.mock import patch

from three_table_quant.domain import ContractError, SourceIssue
from three_table_quant.pipeline import _strict_intersection_or_issue


class PipelineInputBlockedTests(unittest.TestCase):
    def test_contract_failure_becomes_publishable_blocker_without_zero_list(self) -> None:
        tables = [
            type("Table", (), {"decision_date": "20260813"})(),
            type("Table", (), {"decision_date": "20260813"})(),
            type("Table", (), {"decision_date": "20260813"})(),
        ]
        issues: list[SourceIssue] = []
        current_run = {
            "status": "READY",
            "message": "三源日期链已对齐",
            "completed": False,
            "completed_at": None,
            "outcome": "READY",
            "intersection_count": None,
            "target_decision_date": "2026-08-13",
        }

        with patch(
            "three_table_quant.pipeline.strict_intersection",
            side_effect=ContractError(
                "000887.SZ: candidate: estimated_up_limit disagrees"
            ),
        ):
            candidates = _strict_intersection_or_issue(
                tables,
                issues,
                current_run,
            )

        self.assertIsNone(candidates)
        self.assertEqual(current_run["status"], "INPUT_BLOCKED")
        self.assertFalse(current_run["completed"])
        self.assertIsNone(current_run["intersection_count"])
        self.assertEqual(
            current_run["target_decision_date"],
            "2026-08-13",
        )
        self.assertEqual(len(issues), 1)
        issue = issues[0]
        self.assertEqual(issue.code, "STRICT_INTERSECTION_CONTRACT_FAILED")
        self.assertEqual(issue.severity, "error")
        self.assertIn("不删票、不伪造0支", issue.message)
        self.assertIn("000887.SZ", issue.details["error"])
        self.assertEqual(issue.details["decision_dates"], ["20260813"])


if __name__ == "__main__":
    unittest.main()
