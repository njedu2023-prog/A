from __future__ import annotations

import unittest

from three_table_quant.benchmarks import build_batch_benchmarks
from three_table_quant.domain import ContractError


def candidate(code: str, rank: int, borda: float) -> dict:
    return {
        "ts_code": code,
        "name": code,
        "rank": rank,
        "features": {"rank_borda": borda},
    }


class BatchBenchmarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidates = [
            candidate("000001.SZ", 1, 0.20),
            candidate("000002.SZ", 2, 0.90),
            candidate("000003.SZ", 3, 0.60),
        ]
        self.outcomes = {
            "000001.SZ": {
                "is_final": True,
                "net_return_on_allocated": 0.10,
            },
            "000002.SZ": {
                "is_final": True,
                "net_return_on_allocated": -0.02,
            },
            "000003.SZ": {
                "is_final": True,
                "net_return_on_allocated": 0.04,
            },
        }

    def test_same_cohort_policies_use_one_return_basis(self) -> None:
        payload = build_batch_benchmarks(
            "20260807",
            self.candidates,
            self.outcomes,
        )

        policies = payload["policies"]
        self.assertEqual(payload["return_basis"], "net_return_on_allocated")
        self.assertEqual(payload["model_order"], [
            "000001.SZ", "000002.SZ", "000003.SZ"
        ])
        self.assertEqual(payload["borda_order"], [
            "000002.SZ", "000003.SZ", "000001.SZ"
        ])
        self.assertEqual(policies["cash"]["portfolio_return"], 0.0)
        self.assertAlmostEqual(
            policies["all_candidates_equal_weight"]["portfolio_return"],
            0.04,
        )
        self.assertAlmostEqual(
            policies["fixed_model_rank_1"]["portfolio_return"], 0.10
        )
        self.assertAlmostEqual(
            policies["fixed_model_rank_2"]["portfolio_return"], -0.02
        )
        self.assertAlmostEqual(
            policies["fixed_model_rank_3"]["portfolio_return"], 0.04
        )
        self.assertAlmostEqual(
            policies["model_top2_equal_weight"]["portfolio_return"], 0.04
        )
        self.assertAlmostEqual(
            policies["borda_top2_equal_weight"]["portfolio_return"], 0.01
        )

    def test_pending_label_never_becomes_a_partial_portfolio_return(self) -> None:
        self.outcomes["000002.SZ"] = {
            "is_final": False,
            "net_return_on_allocated": None,
        }
        policies = build_batch_benchmarks(
            "20260807", self.candidates, self.outcomes
        )["policies"]

        self.assertIsNone(
            policies["all_candidates_equal_weight"]["portfolio_return"]
        )
        self.assertTrue(policies["fixed_model_rank_1"]["is_final"])
        self.assertFalse(policies["fixed_model_rank_2"]["is_final"])
        self.assertIsNone(
            policies["model_top2_equal_weight"]["portfolio_return"]
        )
        self.assertIsNone(
            policies["borda_top1_equal_weight"]["portfolio_return"]
        )
        self.assertEqual(policies["cash"]["portfolio_return"], 0.0)

    def test_zero_candidate_batch_is_cash_not_missing_data(self) -> None:
        payload = build_batch_benchmarks("20260807", [], {})
        self.assertTrue(payload["is_final"])
        self.assertEqual(
            payload["policies"]["all_candidates_equal_weight"][
                "portfolio_return"
            ],
            0.0,
        )
        self.assertEqual(
            payload["policies"]["fixed_model_rank_1"]["portfolio_return"],
            0.0,
        )

    def test_outcome_cohort_must_exactly_match_frozen_candidates(self) -> None:
        outcomes = dict(self.outcomes)
        outcomes.pop("000003.SZ")
        with self.assertRaisesRegex(ContractError, "cohort mismatch"):
            build_batch_benchmarks("20260807", self.candidates, outcomes)

    def test_legacy_borda_uses_exact_unequal_frozen_source_depths(self) -> None:
        candidates = [
            {
                "ts_code": "000001.SZ",
                "name": "A",
                "rank": 1,
                "features": {},
                "source_ranks": {
                    "a_top10": 1,
                    "premium_top10": 1,
                    "decision_table": 1,
                },
            },
            {
                "ts_code": "000002.SZ",
                "name": "B",
                "rank": 2,
                "features": {},
                "source_ranks": {
                    "a_top10": 2,
                    "premium_top10": 3,
                    "decision_table": 4,
                },
            },
        ]
        outcomes = {
            code: {"is_final": True, "net_return_on_allocated": 0.0}
            for code in ("000001.SZ", "000002.SZ")
        }
        payload = build_batch_benchmarks(
            "20260807",
            candidates,
            outcomes,
            source_table_sizes={
                "a_top10": 10,
                "premium_top10": 6,
                "decision_table": 4,
            },
        )

        first = payload["policies"]["fixed_model_rank_1"]["constituents"][0]
        second = payload["policies"]["fixed_model_rank_2"]["constituents"][0]
        self.assertAlmostEqual(first["borda_score"], 1.0)
        self.assertAlmostEqual(second["borda_score"], 14 / 20)
        self.assertEqual(payload["borda_order"], ["000001.SZ", "000002.SZ"])

    def test_legacy_borda_rejects_missing_source_depth(self) -> None:
        legacy = [
            {
                "ts_code": "000001.SZ",
                "name": "A",
                "rank": 1,
                "features": {},
                "source_ranks": {
                    "a_top10": 1,
                    "premium_top10": 1,
                    "decision_table": 1,
                },
            }
        ]
        outcomes = {
            "000001.SZ": {
                "is_final": True,
                "net_return_on_allocated": 0.0,
            }
        }
        with self.assertRaisesRegex(ContractError, "decision_table table depth"):
            build_batch_benchmarks(
                "20260807",
                legacy,
                outcomes,
                source_table_sizes={"a_top10": 10, "premium_top10": 6},
            )


if __name__ == "__main__":
    unittest.main()
