from __future__ import annotations

import copy
import unittest
from unittest.mock import Mock, patch

from three_table_quant.scheduled_validation import (
    _automation_runs,
    _due_obligations,
    _settle_validation_batch,
    _validation_context,
    _validation_clock,
    _validation_summary,
    run_scheduled_validation,
)


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
        self.assertEqual(
            settle.call_args.kwargs["asof_at"].isoformat(timespec="seconds"),
            "2026-08-10T19:00:08+08:00",
        )
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
            result["automation_runs"]["validation"]["market_date"],
            "2026-08-10",
        )
        self.assertEqual(
            result["automation_runs"]["validation"]["asof_at"],
            "2026-08-10T19:00:08+08:00",
        )
        self.assertEqual(
            result["automation_runs"]["validation"]["result_status"],
            "SUCCESS_NO_DUE",
        )
        for field_name in ("due", "final", "pending_data", "delayed", "failed"):
            self.assertEqual(
                result["automation_runs"]["validation"][field_name],
                0,
            )
        self.assertEqual(
            result["automation_runs"]["output"]["last_completed_at"],
            "2026-08-07T23:09:02+08:00",
        )

    def test_due_obligations_reconcile_final_pending_and_delayed(self) -> None:
        now = _validation_clock("2026-08-10T19:00:08+08:00")
        state = {
            "trades": [
                {
                    "trade_id": "t-final-entry",
                    "buy_date": "20260810",
                    "planned_exit_date": "20260811",
                    "status": "PENDING_BUY",
                    "buy": None,
                    "t_day_validation": {
                        "status": "PENDING",
                        "trade_date": "20260810",
                    },
                },
                {
                    "trade_id": "t-delayed-exit",
                    "buy_date": "20260807",
                    "planned_exit_date": "20260810",
                    "status": "EXIT_DELAYED",
                    "buy": {"price": 10.0},
                    "t_day_validation": {"status": "VERIFIED"},
                },
                {
                    "trade_id": "t-pending-entry",
                    "buy_date": "20260810",
                    "planned_exit_date": "20260811",
                    "status": "BUY_UNVERIFIABLE",
                    "buy": None,
                    "t_day_validation": {"status": "VERIFIED"},
                },
                {
                    "trade_id": "t-final-exit",
                    "buy_date": "20260807",
                    "planned_exit_date": "20260810",
                    "status": "OPEN",
                    "buy": {"price": 10.0},
                    "t_day_validation": {"status": "VERIFIED"},
                },
            ]
        }
        obligations = _due_obligations(state, now, {})
        self.assertEqual(len(obligations), 5)

        state["trades"][0]["status"] = "BUY_UNFILLED"
        state["trades"][0]["t_day_validation"]["status"] = "VERIFIED"
        state["trades"][3]["status"] = "CLOSED"
        summary = _validation_summary(obligations, state)
        self.assertEqual(
            summary,
            {
                "due": 5,
                "final": 3,
                "pending_data": 1,
                "delayed": 1,
                "failed": 0,
                "result_status": "DEGRADED",
                "batch_error": None,
            },
        )

    def test_historical_open_day_uses_1900_asof_and_real_completion_time(self) -> None:
        completed_at = "2026-08-12T00:06:30+08:00"
        context = _validation_context(
            completed_at,
            "20260811",
            {"trading_calendar_path": "data/trading_calendar_2026.json"},
        )
        self.assertEqual(context["market_date"], "20260811")
        self.assertEqual(context["asof_at"], "2026-08-11T19:00:00+08:00")

        runs = _automation_runs(
            {},
            completed_at=completed_at,
            market_date=context["market_date"],
            asof_at=context["asof_at"],
            state={"trades": []},
            validation_summary={
                "due": 0,
                "final": 0,
                "pending_data": 0,
                "delayed": 0,
                "failed": 0,
                "result_status": "SUCCESS_NO_DUE",
                "batch_error": None,
            },
        )
        validation = runs["validation"]
        self.assertEqual(validation["market_date"], "2026-08-11")
        self.assertEqual(validation["asof_at"], "2026-08-11T19:00:00+08:00")
        self.assertEqual(validation["last_attempted_at"], completed_at)
        self.assertEqual(validation["last_completed_at"], completed_at)

    def test_validation_context_rejects_future_closed_and_early_same_day(self) -> None:
        execution = {"trading_calendar_path": "data/trading_calendar_2026.json"}
        with self.assertRaisesRegex(ValueError, "future"):
            _validation_context(
                "2026-08-12T00:06:30+08:00",
                "20260813",
                execution,
            )
        with self.assertRaisesRegex(ValueError, "before 19:00"):
            _validation_context(
                "2026-08-12T18:59:59+08:00",
                "20260812",
                execution,
            )
        with self.assertRaisesRegex(ValueError, "OPEN"):
            _validation_context(
                "2026-08-12T20:00:00+08:00",
                "20260809",
                execution,
            )

    def test_same_day_at_or_after_1900_uses_real_asof(self) -> None:
        context = _validation_context(
            "2026-08-12T19:07:08+08:00",
            "20260812",
            {"trading_calendar_path": "data/trading_calendar_2026.json"},
        )
        self.assertEqual(context["market_date"], "20260812")
        self.assertEqual(context["asof_at"], "2026-08-12T19:07:08+08:00")

    def test_settlement_exception_rolls_back_and_marks_due_failed(self) -> None:
        state = {
            "schema_version": "state_v1",
            "signals": [],
            "trades": [
                {
                    "trade_id": "t-entry",
                    "buy_date": "20260810",
                    "planned_exit_date": "20260811",
                    "status": "PENDING_BUY",
                    "buy": None,
                    "t_day_validation": {"status": "VERIFIED"},
                }
            ],
        }
        original = copy.deepcopy(state)
        with patch(
            "three_table_quant.scheduled_validation.settle_trades",
            side_effect=RuntimeError("provider exploded"),
        ):
            summary = _settle_validation_batch(
                state,
                {"auctions": {}},
                Mock(),
                {},
                "2026-08-10T19:00:08+08:00",
            )
        self.assertEqual(state, original)
        self.assertEqual(summary["due"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["result_status"], "DEGRADED")
        self.assertIn("provider exploded", summary["batch_error"])

    def test_delayed_only_batch_is_degraded(self) -> None:
        obligations = [{"trade_id": "t-delayed", "kind": "EXIT"}]
        state = {
            "trades": [
                {
                    "trade_id": "t-delayed",
                    "status": "EXIT_DELAYED",
                }
            ]
        }
        summary = _validation_summary(obligations, state)
        self.assertEqual(summary["delayed"], 1)
        self.assertEqual(summary["result_status"], "DEGRADED")

    def test_all_due_obligations_final_is_success(self) -> None:
        obligations = [{"trade_id": "t-final", "kind": "T_DAY"}]
        state = {
            "trades": [
                {
                    "trade_id": "t-final",
                    "status": "BUY_UNFILLED",
                    "t_day_validation": {"status": "VERIFIED"},
                }
            ]
        }
        summary = _validation_summary(obligations, state)
        self.assertEqual(summary["final"], 1)
        self.assertEqual(summary["result_status"], "SUCCESS")


if __name__ == "__main__":
    unittest.main()
