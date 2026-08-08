from __future__ import annotations

import copy
import unittest
from unittest.mock import Mock, patch

from three_table_quant.pipeline import _freeze_single_stock_research

from tests.test_single_stock_collection import ASOF, DAY, candidate, execution, minutes


def decision_core(item: object) -> dict:
    payload = copy.deepcopy(item.to_dict())  # type: ignore[attr-defined]
    payload.pop("single_stock_research", None)
    return payload


class PipelineSingleStockResearchTests(unittest.TestCase):
    def test_success_adds_evidence_without_changing_frozen_decision(self) -> None:
        item = candidate()
        before = decision_core(item)
        market = Mock()
        market.minute_bars.return_value = minutes()
        issues: list = []

        _freeze_single_stock_research(
            [item],
            market=market,
            decision_date=DAY,
            decision_asof=ASOF,
            execution=execution(),
            source_issues=issues,
        )

        self.assertEqual(decision_core(item), before)
        self.assertEqual(item.single_stock_research["decision_date"], DAY)
        self.assertEqual(issues, [])

    def test_minute_failure_still_freezes_unknown_research_and_keeps_candidate(self) -> None:
        item = candidate()
        before = decision_core(item)
        market = Mock()
        market.minute_bars.side_effect = RuntimeError("provider unavailable")
        issues: list = []

        _freeze_single_stock_research(
            [item],
            market=market,
            decision_date=DAY,
            decision_asof=ASOF,
            execution=execution(),
            source_issues=issues,
        )

        self.assertEqual(decision_core(item), before)
        self.assertEqual(
            item.single_stock_research["limit_lifecycle"]["availability"],
            "UNAVAILABLE",
        )
        self.assertEqual([issue.severity for issue in issues], ["warning"])

    def test_research_builder_failure_is_warning_not_candidate_failure(self) -> None:
        item = candidate()
        before = decision_core(item)
        issues: list = []
        with patch(
            "three_table_quant.pipeline.build_candidate_single_stock_research",
            side_effect=RuntimeError("audit builder failed"),
        ):
            _freeze_single_stock_research(
                [item],
                market=Mock(minute_bars=Mock(return_value=minutes())),
                decision_date=DAY,
                decision_asof=ASOF,
                execution=execution(),
                source_issues=issues,
            )

        self.assertEqual(decision_core(item), before)
        self.assertEqual(
            item.single_stock_research["schema_version"],
            "single_stock_research_audit_v1",
        )
        self.assertEqual(item.single_stock_research["availability"], "UNAVAILABLE")
        self.assertIsNone(item.single_stock_research["single_stock"])
        self.assertIn(
            "SINGLE_STOCK_RESEARCH_BUILD_FAILED",
            item.single_stock_research["unavailable_reason"],
        )
        self.assertEqual(item.single_stock_research["ts_code"], item.ts_code)
        self.assertEqual(issues[0].code, "SINGLE_STOCK_RESEARCH_BUILD_FAILED")
        self.assertEqual(issues[0].severity, "warning")

    def test_zero_candidates_never_requests_minute_data(self) -> None:
        market = Mock()
        _freeze_single_stock_research(
            [],
            market=market,
            decision_date=DAY,
            decision_asof=ASOF,
            execution=execution(),
            source_issues=[],
        )
        market.minute_bars.assert_not_called()


if __name__ == "__main__":
    unittest.main()
