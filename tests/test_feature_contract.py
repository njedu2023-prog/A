from __future__ import annotations

import unittest
from unittest.mock import patch

from three_table_quant.domain import Candidate
from three_table_quant.feature_contract import (
    FEATURE_CONTRACT_VERSION,
    MODEL_ELIGIBLE_FEATURES,
    PRODUCTION_FEATURE_CONTRACT,
    AsOfPolicy,
    FieldRole,
    MissingPolicy,
    TrustLevel,
    feature_contract_payload,
    feature_contract_sha256,
    model_eligible_feature_names,
    production_feature_coverage,
)
from three_table_quant.features import build_feature_snapshot
from three_table_quant.market import Bar
from three_table_quant.model_training import FEATURE_ALLOWLIST
from three_table_quant.sources import SOURCE_A, SOURCE_DECISION, SOURCE_PREMIUM


class FeatureContractTests(unittest.TestCase):
    def test_contract_covers_current_learning_order_and_is_immutable(self) -> None:
        self.assertEqual(FEATURE_CONTRACT_VERSION, "feature_contract_v2_2")
        self.assertTrue(set(FEATURE_ALLOWLIST) <= set(PRODUCTION_FEATURE_CONTRACT))
        with self.assertRaises(TypeError):
            PRODUCTION_FEATURE_CONTRACT["new_field"] = PRODUCTION_FEATURE_CONTRACT[  # type: ignore[index]
                "ret_5d"
            ]

    def test_model_eligible_set_is_small_preregistered_and_contract_driven(self) -> None:
        expected = (
            "ret_1d",
            "ret_5d",
            "ret_20d",
            "cvar_loss_10pct",
            "max_drawdown_20d",
            "avg_amount_20d",
            "avg_volume_20d",
            "rank_percentile_a_top10",
            "rank_percentile_premium_top10",
            "rank_percentile_decision_table",
            "rank_disagreement",
            "stage_is_2_to_3",
            "stage_is_3_to_4",
        )
        self.assertEqual(MODEL_ELIGIBLE_FEATURES, expected)
        self.assertEqual(model_eligible_feature_names(), expected)
        self.assertEqual(FEATURE_ALLOWLIST, expected)
        self.assertLessEqual(len(FEATURE_ALLOWLIST), 15)
        for name in expected:
            spec = PRODUCTION_FEATURE_CONTRACT[name]
            self.assertTrue(spec.model_eligible, name)
            self.assertNotEqual(spec.trust_level, TrustLevel.UPSTREAM_DECLARED)

        forbidden = {
            "source_strength",
            "rank_borda",
            "rank_consensus",
            "stage_from",
            "stage_to",
        }
        self.assertTrue(forbidden.isdisjoint(FEATURE_ALLOWLIST))
        self.assertFalse(
            any(
                spec.model_eligible
                for spec in PRODUCTION_FEATURE_CONTRACT.values()
                if spec.trust_level == TrustLevel.UPSTREAM_DECLARED
            )
        )

    def test_contract_declares_role_source_asof_missingness_and_trust(self) -> None:
        market = PRODUCTION_FEATURE_CONTRACT["ret_5d"]
        self.assertEqual(market.role, FieldRole.ALPHA)
        self.assertEqual(market.source, "market_daily_qfq")
        self.assertEqual(market.asof, AsOfPolicy.D_CLOSE)
        self.assertEqual(
            market.missing_policy,
            MissingPolicy.REQUIRED_FOR_COVERAGE,
        )
        self.assertEqual(market.trust_level, TrustLevel.LOCAL_POINT_IN_TIME)
        self.assertTrue(market.model_eligible)

        upstream = PRODUCTION_FEATURE_CONTRACT[
            "src_decision_table__predicted_net_return"
        ]
        self.assertEqual(upstream.source, SOURCE_DECISION)
        self.assertEqual(upstream.asof, AsOfPolicy.D_SOURCE_SNAPSHOT)
        self.assertEqual(upstream.trust_level, TrustLevel.UPSTREAM_DECLARED)
        self.assertFalse(upstream.model_eligible)

    def test_contract_fingerprint_is_canonical_and_complete(self) -> None:
        payload = feature_contract_payload()
        self.assertEqual(payload["schema_version"], FEATURE_CONTRACT_VERSION)
        self.assertEqual(
            [item["name"] for item in payload["fields"]],
            sorted(PRODUCTION_FEATURE_CONTRACT),
        )
        self.assertTrue(
            all(isinstance(item["model_eligible"], bool) for item in payload["fields"])
        )
        digest = feature_contract_sha256()
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, feature_contract_sha256())

    def test_coverage_preserves_nine_group_production_semantics(self) -> None:
        complete = {
            "ret_5d": 0.0,
            "ret_20d": 0.01,
            "volatility_20d": 0.02,
            "downside_volatility_20d": 0.01,
            "atr_14d": 0.03,
            "amplitude_20d": 0.04,
            "cvar_loss_10pct": 0.0,
            "max_drawdown_20d": 0.0,
            "avg_amount_20d": 100_000_000.0,
        }
        self.assertEqual(production_feature_coverage(complete), 1.0)

        volume_fallback = dict(complete)
        volume_fallback["avg_amount_20d"] = None
        volume_fallback["avg_volume_20d"] = 1_000_000.0
        self.assertEqual(production_feature_coverage(volume_fallback), 1.0)

        missing_liquidity = dict(volume_fallback)
        missing_liquidity["avg_volume_20d"] = None
        self.assertEqual(production_feature_coverage(missing_liquidity), 8 / 9)

        missing_required = dict(complete)
        missing_required["ret_20d"] = None
        self.assertEqual(production_feature_coverage(missing_required), 8 / 9)

        # Optional rank and upstream predictors are explicitly contracted but
        # do not silently alter the established market-quality coverage score.
        complete["rank_consensus"] = None
        complete["src_decision_table__predicted_net_return"] = None
        self.assertEqual(production_feature_coverage(complete), 1.0)

    def test_feature_builder_delegates_coverage_to_contract(self) -> None:
        candidate = Candidate(
            ts_code="000001.SZ",
            name="test",
            source_ranks={SOURCE_A: 1, SOURCE_PREMIUM: 1, SOURCE_DECISION: 1},
            source_values={
                SOURCE_A: {},
                SOURCE_PREMIUM: {},
                SOURCE_DECISION: {"stage_transition": "2→3"},
            },
        )
        bars = [
            Bar(
                date=f"202607{day:02d}",
                time=None,
                open=10.0,
                close=10.0,
                high=10.1,
                low=9.9,
                volume=1_000_000.0,
                amount=100_000_000.0,
                turnover=2.0,
                provider="TEST",
                price_adjustment="QFQ",
            )
            for day in range(1, 22)
        ]
        bars.append(
            Bar(
                date="20260804",
                time=None,
                open=10.0,
                close=10.0,
                high=10.1,
                low=9.9,
                volume=1_000_000.0,
                amount=100_000_000.0,
                turnover=2.0,
                provider="TEST",
                price_adjustment="QFQ",
            )
        )
        with patch(
            "three_table_quant.features.production_feature_coverage",
            return_value=0.625,
        ) as coverage:
            snapshot = build_feature_snapshot(
                candidate,
                bars,
                {SOURCE_A: 10, SOURCE_PREMIUM: 10, SOURCE_DECISION: 10},
                decision_date="20260804",
            )
        self.assertTrue(snapshot.market_data_valid)
        self.assertEqual(snapshot.coverage, 0.625)
        coverage.assert_called_once()


if __name__ == "__main__":
    unittest.main()
