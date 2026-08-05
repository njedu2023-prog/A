from __future__ import annotations

import copy
import unittest
from datetime import date, timedelta

from three_table_quant.domain import ContractError
from three_table_quant.features import FEATURE_SCHEMA_VERSION
from three_table_quant.model_training import (
    FEATURE_ALLOWLIST,
    MODEL_ARTIFACT_SCHEMA,
    build_promotion_report,
    fit_normalization,
    numeric_feature_rows,
    predict_artifact,
    train_challenger,
    transform_rows,
    walk_forward_oof_report,
)
from three_table_quant.ranking_engine import LearnedChallenger


def trading_dates(count: int, start: date = date(2025, 1, 2)) -> list[str]:
    values: list[str] = []
    cursor = start
    while len(values) < count:
        if cursor.weekday() < 5:
            values.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return values


def training_row(
    index: int,
    decision_date: str,
    rank: int,
    *,
    mature: bool = True,
    force_fill: int | None = None,
) -> dict:
    fill = index % 2 if force_fill is None else force_fill
    delay = (index // 2) % 2
    promotion = (index // 3) % 2
    conditional_return = ((index % 11) - 5) / 500.0 + (0.004 if fill else -0.002)
    features = {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "ret_1d": ((index % 7) - 3) / 100.0,
        "ret_5d": ((index % 13) - 6) / 50.0,
        "ret_20d": ((index % 17) - 8) / 40.0,
        "volatility_5d": 0.01 + (index % 5) / 200.0,
        "volatility_20d": 0.02 + (index % 7) / 200.0,
        "downside_volatility_20d": 0.01 + (index % 3) / 200.0,
        "cvar_loss_10pct": (index % 5) / 100.0,
        "max_drawdown_20d": -(index % 6) / 50.0,
        "rank_borda": 1.0 - rank / 12.0,
        "rank_consensus": 1.0 - rank / 15.0,
        "rank_disagreement": rank / 20.0,
        "stage_is_2_to_3": float(index % 2 == 0),
        "stage_is_3_to_4": float(index % 2 == 1),
        "source_strength": 0.2 + (index % 10) / 20.0,
        "src_a_top10__prob_final": 0.15 + (index % 8) / 20.0,
        "src_premium_top10__premium_rank_score": 35.0 + index % 30,
        "src_decision_table__decision_p_fill": 0.2 + (index % 6) / 10.0,
        # Must never enter the training matrix.
        "actual_future_return": 999.0,
    }
    if index % 9 == 0:
        features["ret_5d"] = None
    return {
        "row_id": f"{decision_date}:R{rank}:{index}",
        "signal_id": f"{decision_date}-signal",
        "candidate_id": f"{decision_date}:{index:06d}.SZ",
        "decision_date": decision_date,
        "buy_date": decision_date,
        "planned_exit_date": decision_date,
        "rank": rank,
        "ts_code": f"{index % 1_000_000:06d}.SZ",
        "feature_asof": decision_date,
        "feature_version": FEATURE_SCHEMA_VERSION,
        "model_version": "transparent_shadow_champion_v2",
        "features": features,
        "labels": {
            "fill": fill,
            "conditional_net_return": conditional_return,
            "policy_net_return": conditional_return if fill else 0.0,
            "promotion": promotion,
            "exit_delayed": delay,
            "exit_delay_days": 1 + delay,
            "label_end_date": decision_date if mature else None,
            "is_mature": mature,
        },
    }


def rows_for_days(days: int, *, force_fill: int | None = None) -> list[dict]:
    rows: list[dict] = []
    index = 0
    for decision_date in trading_dates(days):
        for rank in (1, 2, 3):
            rows.append(
                training_row(
                    index,
                    decision_date,
                    rank,
                    force_fill=force_fill,
                )
            )
            index += 1
    return rows


class FeatureMatrixTests(unittest.TestCase):
    def test_only_whitelisted_numeric_features_enter_matrix(self) -> None:
        rows = rows_for_days(1)
        matrix = numeric_feature_rows(rows)
        self.assertEqual(len(matrix), 3)
        self.assertEqual(len(matrix[0]), len(FEATURE_ALLOWLIST))
        self.assertNotIn("actual_future_return", FEATURE_ALLOWLIST)
        self.assertTrue(
            all(value is None or isinstance(value, float) for value in matrix[0])
        )

    def test_median_imputation_and_standardization_are_fitted_on_train_only(self) -> None:
        first = training_row(1, "20250102", 1)
        second = training_row(2, "20250103", 2)
        first["features"]["ret_1d"] = 1.0
        second["features"]["ret_1d"] = 3.0
        order = ("ret_1d",)
        normalization = fit_normalization([first, second], order)
        self.assertEqual(normalization["median"], [2.0])
        self.assertEqual(normalization["mean"], [2.0])
        self.assertEqual(normalization["scale"], [1.0])

        validation = training_row(3, "20250106", 3)
        validation["features"]["ret_1d"] = 1000.0
        self.assertEqual(
            fit_normalization([first, second], order),
            normalization,
        )
        missing = copy.deepcopy(validation)
        missing["features"]["ret_1d"] = None
        self.assertEqual(
            transform_rows([missing], order, normalization),
            [[0.0]],
        )


class ModelTrainingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.eligible_rows = rows_for_days(126)
        # The model cutoff follows label availability, not merely signal D.
        cls.eligible_rows[-1]["labels"]["label_end_date"] = "20250630"
        cls.validated = train_challenger(
            cls.eligible_rows,
            validation_passed=True,
        )

    def test_small_samples_are_rejected_but_one_class_fill_is_compatible(self) -> None:
        pending = rows_for_days(1)
        for item in pending:
            item["labels"]["is_mature"] = False
            item["labels"]["label_end_date"] = None
        result = train_challenger(pending)
        self.assertEqual(result["status"], "NOT_ELIGIBLE")
        self.assertIsNone(result["artifact"])
        self.assertIn("mature_candidates_below_180", result["reasons"])

        one_class = rows_for_days(126, force_fill=1)
        result = train_challenger(one_class)
        self.assertEqual(result["status"], "TRAINED_UNVALIDATED")
        self.assertEqual(
            result["artifact"]["entry_fill_policy"],
            "T_DAILY_OPEN_FULL_FILL",
        )
        self.assertEqual(
            predict_artifact(
                result["artifact"],
                one_class[0]["features"],
                require_validated=False,
            )["p_fill"],
            1.0,
        )

        wrong_schema = rows_for_days(126)
        for item in wrong_schema:
            item["feature_version"] = "legacy_features_v1"
        result = train_challenger(wrong_schema)
        self.assertEqual(result["status"], "NOT_ELIGIBLE")
        self.assertIn("unsupported_feature_schema_version", result["reasons"])

    def test_artifact_matches_learned_engine_contract(self) -> None:
        result = self.validated
        self.assertEqual(result["status"], "TRAINED_VALIDATED")
        artifact = result["artifact"]
        self.assertEqual(artifact["schema"], MODEL_ARTIFACT_SCHEMA)
        self.assertEqual(artifact["feature_schema"], FEATURE_SCHEMA_VERSION)
        self.assertTrue(artifact["validation_passed"])
        self.assertEqual(
            artifact["entry_fill_policy"],
            "T_DAILY_OPEN_FULL_FILL",
        )
        self.assertEqual(
            len(artifact["feature_order"]),
            len(artifact["normalization"]["median"]),
        )
        self.assertEqual(
            set(artifact["heads"]),
            {
                "fill",
                "delay",
                "promotion",
                "return_mean",
                "q10",
                "q50",
                "q90",
                "delay_days",
            },
        )
        expected_types = {
            "fill": "l2_logistic",
            "delay": "l2_logistic",
            "promotion": "l2_logistic",
            "return_mean": "huber_linear",
            "q10": "pinball_linear",
            "q50": "pinball_linear",
            "q90": "pinball_linear",
            "delay_days": "positive_linear",
        }
        for name, expected_type in expected_types.items():
            head = artifact["heads"][name]
            self.assertEqual(head["type"], expected_type)
            self.assertEqual(
                len(head["coefficients"]),
                len(artifact["feature_order"]),
            )
        self.assertEqual(artifact["trained_through"], "20250630")

    def test_training_and_predictions_are_deterministic_and_quantiles_monotone(self) -> None:
        pending = training_row(9999, "20250701", 1, mature=False)
        pending["features"]["ret_1d"] = 1_000_000.0
        repeated = train_challenger(
            [pending, *reversed(self.eligible_rows)],
            validation_passed=True,
        )
        self.assertEqual(repeated, self.validated)
        artifact = self.validated["artifact"]
        prediction = predict_artifact(
            artifact,
            self.eligible_rows[0]["features"],
        )
        self.assertLessEqual(
            prediction["conditional_net_return_q10"],
            prediction["conditional_net_return_q50"],
        )
        self.assertLessEqual(
            prediction["conditional_net_return_q50"],
            prediction["conditional_net_return_q90"],
        )
        for key in ("p_fill", "p_exit_delay", "p_promotion"):
            self.assertGreaterEqual(prediction[key], 0.0)
            self.assertLessEqual(prediction[key], 1.0)
        self.assertEqual(prediction["p_fill"], 1.0)
        self.assertGreater(prediction["expected_delay_days"], 0.0)

    def test_artifact_is_consumed_by_formal_ranking_engine(self) -> None:
        ranking = {
            "assume_open_fill": True,
            "min_fill_probability": 0.40,
            "min_expected_net_return": 0.0,
            "min_return_lcb": 0.0,
            "min_utility_score": 0.0,
            "max_exit_delay_probability": 0.50,
            "max_missing_fraction": 0.34,
            "cvar_weight": 0.25,
            "exit_delay_weight": 0.005,
            "uncertainty_weight": 0.003,
        }
        challenger = LearnedChallenger(
            self.validated["artifact"],
            ranking,
            estimated_round_trip_rate=0.00162,
        )
        prediction = challenger.predict(
            self.eligible_rows[0]["features"],
            missing_fraction=0.0,
            decision_date="20250701",
        )
        self.assertEqual(
            prediction.model_id,
            self.validated["artifact"]["model_id"],
        )
        self.assertEqual(prediction.model_stage, "LEARNED_CHALLENGER")

    def test_unvalidated_artifact_is_refused_by_default(self) -> None:
        result = train_challenger(self.eligible_rows, validation_passed=False)
        self.assertEqual(result["status"], "TRAINED_UNVALIDATED")
        self.assertFalse(result["artifact"]["validation_passed"])
        with self.assertRaisesRegex(ContractError, "not passed validation"):
            predict_artifact(
                result["artifact"],
                self.eligible_rows[0]["features"],
            )
        prediction = predict_artifact(
            result["artifact"],
            self.eligible_rows[0]["features"],
            require_validated=False,
        )
        self.assertIn("p_fill", prediction)

    def test_walk_forward_report_contains_oof_calibration_and_strategy_risk(self) -> None:
        rows = rows_for_days(14)
        report = walk_forward_oof_report(
            rows,
            min_train_days=10,
            validation_days=1,
            lockbox_days=0,
        )
        self.assertEqual(report["fold_count"], 4)
        self.assertGreater(report["used_fold_count"], 0)
        self.assertGreater(report["prediction_count"], 0)
        self.assertNotIn("fill", report["probability_metrics"])
        for head in ("delay", "promotion"):
            metrics = report["probability_metrics"][head]
            self.assertGreater(metrics["count"], 0)
            self.assertIsNotNone(metrics["brier"])
            self.assertIsNotNone(metrics["logloss"])
            self.assertIsNotNone(metrics["ece"])
        strategy = report["strategy_metrics"]
        self.assertGreater(strategy["daily_count"], 0)
        self.assertIsNotNone(strategy["strategy_after_cost_mean"])
        self.assertLessEqual(strategy["max_drawdown"], 0.0)
        self.assertGreaterEqual(strategy["cvar_loss_10pct"], 0.0)

    def test_promotion_report_never_self_promotes(self) -> None:
        report = build_promotion_report(self.eligible_rows)
        self.assertEqual(report["status"], "PENDING_VALIDATION")
        self.assertFalse(report["validation_passed"])
        self.assertTrue(report["checks"]["sample_gate"])
        self.assertIsNone(report["checks"]["lockbox"])


if __name__ == "__main__":
    unittest.main()
