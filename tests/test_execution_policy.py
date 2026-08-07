from __future__ import annotations

import unittest

from three_table_quant.domain import Candidate, ContractError
from three_table_quant.execution_policy import (
    ORDER_SPEC_SCHEMA,
    build_order_spec,
    validate_truth_against_order_spec,
)


EXECUTION = {
    "auction_time": "09:25",
    "auction_phase": "OPENING_CALL_AUCTION",
    "slot_capital_cny": 100000.0,
    "lot_size": 100,
    "commission_rate": 0.0003,
    "minimum_commission_cny": 5.0,
    "transfer_fee_rate_each_side": 0.00001,
}


def candidate() -> Candidate:
    return Candidate(
        ts_code="000001.SZ",
        name="测试票",
        source_ranks={"a_top10": 1, "premium_top10": 1, "decision_table": 1},
        source_values={
            "decision_table": {
                "stage_transition": "2→3",
                "industry": "测试行业",
                "d_close": 10.0,
                "mechanism_limit_pct": 10.0,
                "estimated_up_limit": 11.0,
            },
            "premium_top10": {"close_T": 10.0},
            "a_top10": {},
        },
    )


class ExecutionPolicyTests(unittest.TestCase):
    def test_order_is_frozen_at_d_limit_and_fits_slot_cash(self) -> None:
        spec = build_order_spec(
            candidate(),
            decision_date="20260804",
            buy_date="20260805",
            execution=EXECUTION,
        )
        self.assertEqual(spec["schema_version"], ORDER_SPEC_SCHEMA)
        self.assertEqual(spec["event_time"], "09:25")
        self.assertEqual(spec["limit_price"], 11.0)
        self.assertEqual(spec["submitted_qty"] % 100, 0)
        self.assertLessEqual(
            spec["maximum_reserved_cash_cny"],
            EXECUTION["slot_capital_cny"],
        )
        self.assertGreater(spec["submitted_qty"], 0)

    def test_half_tick_source_price_freezes_exchange_rounded_limit(self) -> None:
        item = candidate()
        item.source_values["decision_table"].update(
            {
                "d_close": 94.95,
                "estimated_up_limit": 104.44,
            }
        )
        item.source_values["premium_top10"]["close_T"] = 94.95

        spec = build_order_spec(
            item,
            decision_date="20260807",
            buy_date="20260810",
            execution=EXECUTION,
        )

        self.assertEqual(spec["limit_price"], 104.45)
        self.assertEqual(
            spec["price_limit_source"],
            "D_CLOSE_MECHANISM_ROUND_HALF_UP",
        )

    def test_truth_must_match_frozen_quantity_and_limit_price(self) -> None:
        spec = build_order_spec(
            candidate(),
            decision_date="20260804",
            buy_date="20260805",
            execution=EXECUTION,
        )
        validate_truth_against_order_spec(
            spec,
            submitted_qty=spec["submitted_qty"],
            limit_price=11.0,
            price_tick=0.01,
        )
        with self.assertRaisesRegex(ContractError, "submitted_qty"):
            validate_truth_against_order_spec(
                spec,
                submitted_qty=spec["submitted_qty"] - 100,
                limit_price=11.0,
                price_tick=0.01,
            )
        with self.assertRaisesRegex(ContractError, "limit_price"):
            validate_truth_against_order_spec(
                spec,
                submitted_qty=spec["submitted_qty"],
                limit_price=10.99,
                price_tick=0.01,
            )

    def test_candidate_serialization_freezes_order_spec(self) -> None:
        item = candidate()
        item.order_spec = build_order_spec(
            item,
            decision_date="20260804",
            buy_date="20260805",
            execution=EXECUTION,
        )
        self.assertEqual(
            item.to_dict()["order_spec"]["schema_version"],
            ORDER_SPEC_SCHEMA,
        )


if __name__ == "__main__":
    unittest.main()
