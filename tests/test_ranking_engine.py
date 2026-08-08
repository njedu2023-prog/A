from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from three_table_quant.domain import Candidate
from three_table_quant.features import (
    FEATURE_SCHEMA_VERSION,
    build_feature_snapshot,
    extract_whitelisted_source_features,
)
from three_table_quant.market import Bar
from three_table_quant.ranking_engine import (
    ArtifactValidationError,
    LEARNED_ARTIFACT_SCHEMA_VERSION,
    MODEL_ID,
    PREDICTION_SCHEMA_VERSION,
    LearnedChallenger,
    TransparentChampionV2,
    _rank_with_predictor,
)
from three_table_quant.model_registry import artifact_sha256
from three_table_quant.promotion import (
    PROMOTION_REPORT_SCHEMA,
    REQUIRED_PROMOTION_CHECKS,
    artifact_fingerprint,
    attach_promotion_certificate,
)
from three_table_quant.scoring import score_candidates
from three_table_quant.sources import SOURCE_A, SOURCE_DECISION, SOURCE_PREMIUM


TABLE_SIZES = {SOURCE_A: 10, SOURCE_PREMIUM: 10, SOURCE_DECISION: 10}
CONFIG = {
    "ranking": {
        "min_daily_bars": 21,
        "min_fill_probability": 0.40,
        "min_expected_net_return": 0.0,
        "min_return_lcb": 0.0,
        "min_utility_score": 0.0,
        "max_exit_delay_probability": 0.50,
        "max_missing_fraction": 0.34,
        "cvar_weight": 0.25,
        "exit_delay_weight": 0.005,
        "uncertainty_weight": 0.003,
    },
    "execution": {
        "slot_capital_cny": 100000,
        "minimum_commission_cny": 5,
        "exit_minute_count": 5,
        "commission_rate": 0.0003,
        "stamp_duty_sell_rate": 0.0005,
        "transfer_fee_rate_each_side": 0.00001,
        "slippage_rate_each_side": 0.0005,
    },
}


def make_candidate(
    code: str,
    ranks: tuple[int, int, int] = (1, 1, 1),
    *,
    decision_date: str = "20260804",
) -> Candidate:
    return Candidate(
        ts_code=code,
        name=code,
        source_ranks=dict(
            zip((SOURCE_A, SOURCE_PREMIUM, SOURCE_DECISION), ranks, strict=True)
        ),
        source_values={
            SOURCE_A: {
                "trade_date": decision_date,
                "prob_final": 0.30,
                "actual_t_close": 999.0,
                "truth_future": 999.0,
            },
            SOURCE_PREMIUM: {
                "trade_date": decision_date,
                "premium_rank_score": 55.0,
                "t_limitup_prob_calibrated": 0.25,
                "actual_net_return": 9.0,
            },
            SOURCE_DECISION: {
                "stage_transition": "2→3",
                "decision_p_fill": 0.55,
                "predicted_net_return": 0.01,
                "predicted_exit_probability": 0.82,
                "predicted_return_lcb": -0.05,
                "predicted_return_ucb": 0.08,
                "observation_t_return": 0.10,
                "observation_fill": 1,
                "continuation_limit_up_hit": 1,
                "actual_gross_return": 1.0,
            },
        },
    )


def daily_bars(
    count: int = 30,
    *,
    end_day: int = 4,
    flat_or_up: bool = False,
    amount: float = 100_000_000.0,
    volume: float = 1_000_000.0,
) -> list[Bar]:
    # Use July history and the requested August tail so dates stay unique and
    # the final row is exactly D when end_day=4.
    dates = [f"202607{day:02d}" for day in range(1, count)] + [f"202608{end_day:02d}"]
    values: list[Bar] = []
    for index, day in enumerate(dates):
        close = 10.0 * (1.01**index) if flat_or_up else 10.0 + 0.08 * index + (0.2 if index % 3 == 0 else -0.1)
        values.append(
            Bar(
                date=day,
                time=None,
                open=close * 0.99,
                close=close,
                high=close * 1.02,
                low=close * 0.98,
                volume=volume,
                amount=amount,
                turnover=3.0,
                provider="TEST",
                price_adjustment="QFQ",
            )
        )
    return values


def certify_artifact(artifact: dict) -> dict:
    core = copy.deepcopy(artifact)
    core.pop("artifact_fingerprint", None)
    core.pop("promotion_certificate", None)
    core.pop("promotion_state", None)
    report = {
        "schema": PROMOTION_REPORT_SCHEMA,
        "status": "APPROVED",
        "promotion_state": "APPROVED",
        "model_id": core["model_id"],
        "artifact_fingerprint": artifact_fingerprint(core),
        "evaluation_dataset_fingerprint": "c" * 64,
        "checks": {name: True for name in REQUIRED_PROMOTION_CHECKS},
    }
    return attach_promotion_certificate(core, report)


def learned_artifact(*, trained_through: str = "20260803") -> dict:
    feature_order = ["rank_consensus", "ret_5d"]

    def head(kind: str, intercept: float, coefficients: list[float] | None = None) -> dict:
        return {
            "type": kind,
            "intercept": intercept,
            "coefficients": coefficients or [0.0, 0.0],
        }

    artifact = {
        "schema": LEARNED_ARTIFACT_SCHEMA_VERSION,
        "model_id": "walk_forward_challenger_001",
        "feature_schema": FEATURE_SCHEMA_VERSION,
        "trained_through": trained_through,
        "feature_order": feature_order,
        "normalization": {
            "median": [0.5, 0.0],
            "mean": [0.5, 0.0],
            "scale": [0.25, 0.10],
        },
        "heads": {
            "fill": head("l2_logistic", 1.5),
            "return_mean": head("huber_linear", 0.03),
            # Deliberately crossed: inference must project them into order.
            "return_q10": head("pinball_linear", 0.05),
            "return_q50": head("pinball_linear", 0.01),
            "return_q90": head("pinball_linear", 0.03),
            "delay": head("l2_logistic", -2.0),
            "delay_days": head("positive_linear", 0.0),
            "promotion": head("l2_logistic", 0.4),
        },
    }
    return certify_artifact(artifact)


class FeatureContractTests(unittest.TestCase):
    def test_only_explicit_d_asof_source_fields_enter_features(self) -> None:
        item = make_candidate("000001.SZ")
        source_features = extract_whitelisted_source_features(item)
        self.assertIn(f"src_{SOURCE_A}__prob_final", source_features)
        self.assertIn(f"src_{SOURCE_DECISION}__predicted_net_return", source_features)
        self.assertAlmostEqual(
            source_features[
                f"src_{SOURCE_DECISION}__predicted_exit_delay_probability"
            ],
            0.18,
        )
        forbidden_fragments = (
            "actual_",
            "truth_",
            "observation_t_return",
            "observation_fill",
            "continuation_limit_up_hit",
        )
        self.assertFalse(
            any(fragment in key for key in source_features for fragment in forbidden_fragments)
        )

    def test_feature_snapshot_contains_formal_market_rank_and_stage_fields(self) -> None:
        snapshot = build_feature_snapshot(
            make_candidate("000001.SZ", (1, 3, 2)),
            daily_bars(),
            TABLE_SIZES,
            decision_date="20260804",
            min_daily_bars=21,
        ).to_dict()
        self.assertEqual(snapshot["feature_schema_version"], FEATURE_SCHEMA_VERSION)
        self.assertTrue(snapshot["market_data_valid"])
        for field in (
            "ret_1d",
            "ret_3d",
            "ret_5d",
            "ret_10d",
            "ret_20d",
            "volatility_5d",
            "volatility_20d",
            "downside_volatility_20d",
            "atr_14d",
            "amplitude_20d",
            "cvar_loss_10pct",
            "max_drawdown_20d",
            "avg_amount_20d",
            "avg_volume_20d",
            "rank_borda",
            "rank_consensus",
            "rank_disagreement",
        ):
            self.assertIsNotNone(snapshot[field], field)
        self.assertEqual(snapshot["stage_transition"], "2→3")
        self.assertEqual(snapshot["stage_is_2_to_3"], 1.0)

    def test_minimum_bars_and_exact_d_tail_are_fail_closed(self) -> None:
        too_short = build_feature_snapshot(
            make_candidate("000001.SZ"),
            daily_bars(20),
            TABLE_SIZES,
            decision_date="20260804",
            min_daily_bars=21,
        )
        self.assertFalse(too_short.market_data_valid)
        self.assertIn("min_daily_bars_not_met", too_short.invalid_reasons)

        stale = build_feature_snapshot(
            make_candidate("000001.SZ"),
            daily_bars(end_day=3),
            TABLE_SIZES,
            decision_date="20260804",
            min_daily_bars=21,
        )
        self.assertFalse(stale.market_data_valid)
        self.assertIn("last_bar_not_decision_date", stale.invalid_reasons)

    def test_zero_cvar_and_zero_drawdown_remain_real_values(self) -> None:
        item = make_candidate("000001.SZ")
        result = score_candidates(
            [item],
            {item.ts_code: daily_bars(flat_or_up=True)},
            TABLE_SIZES,
            CONFIG,
            decision_date="20260804",
        )[0]
        self.assertEqual(result.features["cvar_loss_10pct"], 0.0)
        self.assertEqual(result.features["max_drawdown_20d"], 0.0)
        self.assertEqual(result.metrics["cvar_loss_10pct"], 0.0)

    def test_amount_and_volume_remain_separate_liquidity_features(self) -> None:
        item = make_candidate("000001.SZ")
        volume_only = daily_bars(amount=0.0, volume=2_000_000.0)
        snapshot = build_feature_snapshot(
            item,
            volume_only,
            TABLE_SIZES,
            decision_date="20260804",
        ).to_dict()
        self.assertIsNone(snapshot["avg_amount_20d"])
        self.assertGreater(snapshot["avg_volume_20d"], 0.0)
        self.assertNotIn("liquidity_value", snapshot)


class RankingEngineTests(unittest.TestCase):
    def test_learned_missingness_uses_the_artifact_feature_order(self) -> None:
        predictor = LearnedChallenger(
            learned_artifact(),
            CONFIG["ranking"],
            estimated_round_trip_rate=0.001,
        )
        complete = make_candidate("000001.SZ")
        complete.features = {
            "market_data_valid": True,
            "feature_coverage": 1.0,
            "rank_consensus": 0.8,
            "rank_borda": 0.8,
            "rank_disagreement": 0.0,
            "ret_5d": 0.01,
        }
        missing = make_candidate("000002.SZ")
        missing.features = {
            "market_data_valid": True,
            "feature_coverage": 1.0,
            "rank_consensus": 0.7,
            "rank_borda": 0.7,
            "rank_disagreement": 0.0,
            "ret_5d": None,
        }

        ranked = _rank_with_predictor(
            [complete, missing],
            CONFIG["ranking"],
            predictor,
            decision_date="20260804",
        )

        self.assertTrue(all(bundle.ranking_fallback for _, bundle in ranked))
        by_code = {candidate.ts_code: bundle for candidate, bundle in ranked}
        self.assertIn(
            "market_features_incomplete",
            by_code["000002.SZ"].gate_reasons,
        )
        self.assertNotIn(
            "market_features_incomplete",
            by_code["000001.SZ"].gate_reasons,
        )

    def test_open_fill_assumption_fixes_fill_to_one_and_removes_fill_gate(self) -> None:
        item = make_candidate("000001.SZ")
        features = build_feature_snapshot(
            item,
            daily_bars(),
            TABLE_SIZES,
            decision_date="20260804",
        ).to_dict()
        ranking = {**CONFIG["ranking"], "assume_open_fill": True}
        prediction = TransparentChampionV2(
            ranking,
            estimated_round_trip_rate=0.00162,
        ).predict(features, missing_fraction=0.0)

        self.assertEqual(prediction.p_fill, 1.0)
        self.assertEqual(prediction.expected_fill_ratio, 1.0)
        self.assertNotIn("p_fill_below_threshold", prediction.gate_reasons)
        expected_utility = (
            prediction.conditional_net_return_mean
            - ranking["cvar_weight"] * prediction.expected_shortfall
            - ranking["exit_delay_weight"]
            * prediction.p_exit_delay
            * prediction.expected_delay_days
            - ranking["uncertainty_weight"] * prediction.uncertainty
        )
        self.assertAlmostEqual(prediction.utility, expected_utility)

    def test_champion_emits_all_formal_heads_and_promotion_is_auxiliary(self) -> None:
        item = make_candidate("000001.SZ")
        features = build_feature_snapshot(
            item,
            daily_bars(),
            TABLE_SIZES,
            decision_date="20260804",
        ).to_dict()
        champion = TransparentChampionV2(
            CONFIG["ranking"],
            estimated_round_trip_rate=0.00162,
        )
        first = champion.predict(features, missing_fraction=0.0)
        self.assertEqual(first.schema_version, PREDICTION_SCHEMA_VERSION)
        self.assertEqual(first.model_id, MODEL_ID)
        self.assertLessEqual(first.conditional_net_return_q10, first.conditional_net_return_q50)
        self.assertLessEqual(first.conditional_net_return_q50, first.conditional_net_return_q90)
        for value in (first.p_fill, first.p_exit_delay, first.p_promotion, first.uncertainty):
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

        changed = dict(features)
        changed[f"src_{SOURCE_A}__p_limit_up_calibrated"] = 0.99
        changed[f"src_{SOURCE_PREMIUM}__t_limitup_prob_calibrated"] = 0.99
        second = champion.predict(changed, missing_fraction=0.0)
        self.assertNotEqual(first.p_promotion, second.p_promotion)
        self.assertEqual(first.utility, second.utility)

    def test_one_incomplete_candidate_switches_cohort_to_borda_and_no_trade(self) -> None:
        best_borda = make_candidate("000001.SZ", (1, 1, 1))
        lower_borda = make_candidate("000002.SZ", (8, 8, 8))
        result = score_candidates(
            [lower_borda, best_borda],
            {
                best_borda.ts_code: daily_bars(),
                lower_borda.ts_code: [],
            },
            TABLE_SIZES,
            CONFIG,
            decision_date="20260804",
        )
        self.assertEqual([item.ts_code for item in result], ["000001.SZ", "000002.SZ"])
        self.assertTrue(all(item.metrics["ranking_fallback"] for item in result))
        self.assertTrue(all(not item.metrics["policy_trade_eligible"] for item in result))
        self.assertTrue(
            all("cohort_ranking_fallback_borda" in item.metrics["gate_reasons"] for item in result)
        )

    def test_legacy_metric_aliases_and_shadow_action_are_preserved(self) -> None:
        item = make_candidate("000001.SZ")
        result = score_candidates(
            [item],
            {item.ts_code: daily_bars()},
            TABLE_SIZES,
            CONFIG,
            decision_date="20260804",
        )[0]
        for key in (
            "p_fill_0925",
            "expected_gross_return",
            "expected_net_return",
            "cvar_loss_10pct",
            "p_exit_delay",
            "uncertainty",
            "utility_score",
            "missing_fraction",
            "policy_trade_eligible",
        ):
            self.assertIn(key, result.metrics)
        self.assertEqual(result.action, "SHADOW")
        self.assertIn("not_a_broker_order", result.action_reason)

    def test_learned_json_inference_is_bounded_monotone_and_uses_same_gate(self) -> None:
        item = make_candidate("000001.SZ")
        resolved = {
            "model": {
                "kind": "TRAINED",
                "model_id": "walk_forward_challenger_001",
            },
            "fallback": False,
            "fallback_reason": None,
        }
        result = score_candidates(
            [item],
            {item.ts_code: daily_bars()},
            TABLE_SIZES,
            CONFIG,
            decision_date="20260804",
            resolved_model=resolved,
            artifact=learned_artifact(),
        )[0]
        prediction = result.metrics["prediction"]
        self.assertEqual(prediction["model_id"], "walk_forward_challenger_001")
        self.assertEqual(prediction["model_stage"], "LEARNED_CHALLENGER")
        self.assertFalse(result.metrics["model_resolution_fallback"])
        self.assertLessEqual(
            prediction["conditional_net_return_q10"],
            prediction["conditional_net_return_q50"],
        )
        self.assertLessEqual(
            prediction["conditional_net_return_q50"],
            prediction["conditional_net_return_q90"],
        )
        for field in ("p_fill", "p_exit_delay", "p_promotion"):
            self.assertGreaterEqual(prediction[field], 0.0)
            self.assertLessEqual(prediction[field], 1.0)
        self.assertGreater(prediction["expected_delay_days"], 0.0)
        self.assertEqual(result.action, "SHADOW")
        self.assertEqual(
            result.metrics["policy_trade_eligible"],
            result.metrics["gate_decision"] == "TRADE",
        )

    def test_resolved_learned_json_is_loaded_from_contained_repo_path(self) -> None:
        item = make_candidate("000001.SZ")
        payload = learned_artifact()
        with tempfile.TemporaryDirectory() as directory:
            model_dir = Path(directory) / "data" / "models"
            model_dir.mkdir(parents=True)
            artifact_path = model_dir / "challenger.json"
            artifact_path.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            resolved = {
                "model": {
                    "kind": "TRAINED",
                    "model_id": payload["model_id"],
                    "artifact_path": "data/models/challenger.json",
                    "artifact_sha256": artifact_sha256(artifact_path),
                },
                "fallback": False,
                "fallback_reason": None,
            }
            result = score_candidates(
                [item],
                {item.ts_code: daily_bars()},
                TABLE_SIZES,
                CONFIG,
                decision_date="20260804",
                resolved_model=resolved,
                model_base_dir=directory,
            )[0]
        self.assertEqual(result.metrics["model_id"], payload["model_id"])
        self.assertFalse(result.metrics["model_resolution_fallback"])

    def test_zero_return_and_zero_lower_bound_do_not_pass_strict_gate(self) -> None:
        item = make_candidate("000001.SZ")
        artifact = learned_artifact()
        artifact["heads"]["return_mean"]["intercept"] = 0.0
        artifact["heads"]["return_q10"]["intercept"] = 0.0
        artifact["heads"]["return_q50"]["intercept"] = 0.0
        artifact["heads"]["return_q90"]["intercept"] = 0.0
        artifact = certify_artifact(artifact)
        result = score_candidates(
            [item],
            {item.ts_code: daily_bars()},
            TABLE_SIZES,
            CONFIG,
            decision_date="20260804",
            resolved_model={
                "model": {
                    "kind": "TRAINED",
                    "model_id": artifact["model_id"],
                },
                "fallback": False,
            },
            artifact=artifact,
        )[0]
        self.assertEqual(result.metrics["gate_decision"], "NO_TRADE")
        self.assertIn(
            "expected_net_return_not_positive",
            result.metrics["gate_reasons"],
        )
        self.assertIn(
            "conditional_return_lcb_not_positive",
            result.metrics["gate_reasons"],
        )

    def test_learned_cutoff_must_precede_d_or_scoring_falls_back_to_champion(self) -> None:
        item = make_candidate("000001.SZ")
        resolved = {
            "model": {
                "kind": "TRAINED",
                "model_id": "walk_forward_challenger_001",
            },
            "fallback": False,
        }
        result = score_candidates(
            [item],
            {item.ts_code: daily_bars()},
            TABLE_SIZES,
            CONFIG,
            decision_date="20260804",
            resolved_model=resolved,
            artifact=learned_artifact(trained_through="20260804"),
        )[0]
        self.assertEqual(result.metrics["model_id"], MODEL_ID)
        self.assertTrue(result.metrics["model_resolution_fallback"])
        self.assertIn(
            "trained_through must be strictly earlier",
            result.metrics["model_resolution_reason"],
        )

    def test_artifact_validation_rejects_future_features_and_uncertified_models(self) -> None:
        future = learned_artifact()
        future["feature_order"] = ["actual_net_return", "ret_5d"]
        with self.assertRaisesRegex(ArtifactValidationError, "future-aware"):
            LearnedChallenger(
                future,
                CONFIG["ranking"],
                estimated_round_trip_rate=0.00162,
            )

        uncertified = learned_artifact()
        uncertified.pop("artifact_fingerprint")
        uncertified.pop("promotion_certificate")
        uncertified.pop("promotion_state")
        uncertified["validation_passed"] = True
        with self.assertRaisesRegex(ArtifactValidationError, "promotion certificate"):
            LearnedChallenger(
                uncertified,
                CONFIG["ranking"],
                estimated_round_trip_rate=0.00162,
            )

    def test_artifact_head_and_normalization_contracts_are_strict(self) -> None:
        missing_head = learned_artifact()
        missing_head["heads"].pop("delay_days")
        with self.assertRaisesRegex(ArtifactValidationError, "heads are incomplete"):
            LearnedChallenger(
                missing_head,
                CONFIG["ranking"],
                estimated_round_trip_rate=0.00162,
            )

        bad_scale = learned_artifact()
        bad_scale["normalization"]["scale"] = [1.0, 0.0]
        with self.assertRaisesRegex(ArtifactValidationError, "scale values must be positive"):
            LearnedChallenger(
                bad_scale,
                CONFIG["ranking"],
                estimated_round_trip_rate=0.00162,
            )


if __name__ == "__main__":
    unittest.main()
