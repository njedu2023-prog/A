from __future__ import annotations

import unittest

from three_table_quant.domain import Candidate
from three_table_quant.market import Bar
from three_table_quant.scoring import estimate_round_trip_rate, score_candidates
from three_table_quant.sources import SOURCE_A, SOURCE_DECISION, SOURCE_PREMIUM


def candidate(code: str, ranks: tuple[int, int, int]) -> Candidate:
    return Candidate(
        ts_code=code,
        name=code,
        source_ranks=dict(zip((SOURCE_A, SOURCE_PREMIUM, SOURCE_DECISION), ranks, strict=True)),
        source_values={
            SOURCE_A: {"prob_final": "0.4"},
            SOURCE_PREMIUM: {"premium_rank_score": "45"},
            SOURCE_DECISION: {"decision_ev": "0.01"},
        },
    )


def bars(multiplier: float) -> list[Bar]:
    values: list[Bar] = []
    for day in range(1, 31):
        close = 10.0 * (1.0 + multiplier * day / 100.0)
        values.append(
            Bar(
                date=f"202607{day:02d}",
                time=None,
                open=close * .99,
                close=close,
                high=close * 1.01,
                low=close * .98,
                volume=100000,
                amount=100000000 * (1 + multiplier),
                turnover=3.0,
            )
        )
    return values


CONFIG = {
    "ranking": {
        "min_fill_probability": .4,
        "min_expected_net_return": 0,
        "min_utility_score": 0,
        "max_missing_fraction": .34,
        "cvar_weight": .25,
        "exit_delay_weight": .005,
        "uncertainty_weight": .003,
    },
    "execution": {
        "slot_capital_cny": 100000,
        "minimum_commission_cny": 5,
        "exit_minute_count": 5,
        "commission_rate": .0003,
        "stamp_duty_sell_rate": .0005,
        "transfer_fee_rate_each_side": .00001,
        "slippage_rate_each_side": .0005,
    },
}


class ScoringTests(unittest.TestCase):
    def test_estimated_cost_matches_one_buy_and_five_exit_children(self) -> None:
        # 30 yuan buy commission + 5 * 6 yuan sell commission, plus taxes,
        # transfer fees and one exit-side slippage deduction.
        self.assertAlmostEqual(estimate_round_trip_rate(CONFIG["execution"]), .00162)

    def test_all_intersection_candidates_are_ranked(self) -> None:
        items = [candidate("600001.SH", (3, 2, 1)), candidate("000001.SZ", (1, 1, 2)), candidate("600002.SH", (2, 3, 3)), candidate("000002.SZ", (4, 4, 4))]
        result = score_candidates(
            items,
            {item.ts_code: bars(index + 1) for index, item in enumerate(items)},
            {SOURCE_A: 10, SOURCE_PREMIUM: 10, SOURCE_DECISION: 20},
            CONFIG,
        )
        self.assertEqual(len(result), 4)
        self.assertEqual([item.rank for item in result], [1, 2, 3, 4])
        self.assertTrue(all(item.action == "SHADOW" for item in result[:3]))
        self.assertEqual(result[3].action, "NO_TRADE")

    def test_missing_market_data_keeps_candidate_but_blocks_policy_gate(self) -> None:
        item = candidate("600001.SH", (1, 1, 1))
        result = score_candidates([item], {}, {SOURCE_A: 10, SOURCE_PREMIUM: 10, SOURCE_DECISION: 20}, CONFIG)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].action, "SHADOW")
        self.assertFalse(result[0].metrics["policy_trade_eligible"])
        self.assertIn("market_features_incomplete", result[0].action_reason)

    def test_daily_volume_is_a_liquidity_proxy_when_amount_is_unavailable(self) -> None:
        item = candidate("600001.SH", (1, 1, 1))
        volume_only = [
            Bar(
                date=bar.date,
                time=None,
                open=bar.open,
                close=bar.close,
                high=bar.high,
                low=bar.low,
                volume=bar.volume,
                amount=0,
            )
            for bar in bars(1)
        ]
        result = score_candidates(
            [item],
            {item.ts_code: volume_only},
            {SOURCE_A: 10, SOURCE_PREMIUM: 10, SOURCE_DECISION: 20},
            CONFIG,
        )
        self.assertIsNone(result[0].features["avg_amount_20d"])
        self.assertGreater(result[0].features["avg_volume_20d"], 0)
        self.assertEqual(result[0].metrics["missing_fraction"], 0)


if __name__ == "__main__":
    unittest.main()
