from __future__ import annotations

import copy
import unittest
from unittest.mock import Mock, patch

from three_table_quant.scheduled_validation import run_scheduled_validation


class ScheduledValidationTests(unittest.TestCase):
    def test_validation_only_settles_without_loading_or_ranking_sources(self) -> None:
        state = {
            "schema_version": "state_v1",
            "signals": [{"signal_id": "frozen-1", "candidates": []}],
            "trades": [],
        }
        original_signals = copy.deepcopy(state["signals"])
        config = {
            "tracked_ranks": [1, 2, 3],
            "execution": {},
            "paths": {
                "state": "state.json",
                "dashboard": "dashboard.json",
                "source_issues": "issues.json",
                "execution_truth": "truth.json",
                "model_registry": "registry.json",
            },
        }
        current_run = {
            "status": "RANKED",
            "completed": True,
            "completed_at": "2026-08-07T23:09:02+08:00",
            "decision_date": "2026-08-07",
            "intersection_count": 4,
        }
        previous_dashboard = {
            "current_run": current_run,
            "automation_runs": {
                "validation": {"scheduled_local_time": "15:20"},
            },
        }

        def load_json(path: str, default: object) -> object:
            return {
                "truth.json": {"schema_version": "execution_truth_v1", "auctions": {}},
                "dashboard.json": previous_dashboard,
                "issues.json": {"schema_version": "source_issues_v1", "issues": []},
            }.get(path, default)

        with (
            patch("three_table_quant.scheduled_validation.load_config", return_value=config),
            patch("three_table_quant.scheduled_validation.load_state", return_value=state),
            patch("three_table_quant.scheduled_validation.load_json", side_effect=load_json),
            patch("three_table_quant.scheduled_validation.HttpClient"),
            patch("three_table_quant.scheduled_validation.ResilientMarketData", return_value=Mock()),
            patch("three_table_quant.scheduled_validation._ensure_all_candidate_shadow_ledger") as ensure,
            patch("three_table_quant.scheduled_validation.settle_trades") as settle,
            patch(
                "three_table_quant.scheduled_validation._refresh_model_registry",
                return_value=({"schema_version": "model_registry_v1"}, {}, None, None),
            ),
            patch(
                "three_table_quant.scheduled_validation.build_dashboard",
                return_value={"schema_version": "dashboard_v1", "current_run": current_run},
            ) as build,
            patch("three_table_quant.scheduled_validation.validate_dashboard") as validate,
            patch("three_table_quant.scheduled_validation.save_json") as save,
            patch(
                "three_table_quant.scheduled_validation._now",
                return_value="2026-08-10T19:00:08+08:00",
            ),
            patch("three_table_quant.pipeline.SourceLoader") as source_loader,
            patch("three_table_quant.pipeline.strict_intersection") as intersection,
            patch("three_table_quant.pipeline.score_candidates") as score,
            patch("three_table_quant.pipeline.add_signal") as add_signal,
        ):
            result = run_scheduled_validation("config/system.json")

        ensure.assert_called_once_with(state, [1, 2, 3])
        settle.assert_called_once()
        build.assert_called_once()
        validate.assert_called_once_with(result)
        self.assertEqual(save.call_count, 3)
        source_loader.assert_not_called()
        intersection.assert_not_called()
        score.assert_not_called()
        add_signal.assert_not_called()
        self.assertEqual(state["signals"], original_signals)
        self.assertIs(result["current_run"], current_run)
        self.assertEqual(
            result["automation_runs"]["validation"]["last_completed_at"],
            "2026-08-10T19:00:08+08:00",
        )
        self.assertEqual(
            result["automation_runs"]["validation"]["scheduled_local_time"],
            "19:00",
        )
        self.assertEqual(
            result["automation_runs"]["output"]["last_completed_at"],
            "2026-08-07T23:09:02+08:00",
        )


if __name__ == "__main__":
    unittest.main()
