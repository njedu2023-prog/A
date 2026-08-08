from __future__ import annotations

import unittest
from types import SimpleNamespace

from three_table_quant.pipeline import _target_date_issue


class TargetDecisionDateTests(unittest.TestCase):
    def tables(self, decision_date: str) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(source_id=source_id, decision_date=decision_date)
            for source_id in ("a_top10", "premium_top10", "decision_table")
        ]

    def test_same_day_sources_are_accepted(self) -> None:
        normalized, issue = _target_date_issue(self.tables("20260810"), "20260810")
        self.assertEqual(normalized, "20260810")
        self.assertIsNone(issue)

    def test_stale_but_internally_aligned_sources_are_blocked(self) -> None:
        normalized, issue = _target_date_issue(self.tables("20260807"), "20260810")
        self.assertEqual(normalized, "20260810")
        self.assertIsNotNone(issue)
        self.assertEqual(issue.code, "SOURCE_TARGET_DATE_NOT_READY")
        self.assertEqual(
            issue.details["observed_decision_dates"],
            {
                "a_top10": "20260807",
                "premium_top10": "20260807",
                "decision_table": "20260807",
            },
        )


if __name__ == "__main__":
    unittest.main()
