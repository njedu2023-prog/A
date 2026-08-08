from __future__ import annotations

import unittest

from three_table_quant.domain import ContractError
from three_table_quant.execution_scenarios import (
    ExecutionScenario,
    ExecutionScenarioInput,
    FeeSchedule,
    evaluate_execution_scenario,
    evaluate_execution_scenarios,
    shadow_open_benchmark,
)


ZERO_FEES = FeeSchedule(
    commission_rate=0.0,
    minimum_commission_cny=0.0,
    stamp_duty_sell_rate=0.0,
    transfer_fee_rate_each_side=0.0,
)


def inputs(**overrides: object) -> ExecutionScenarioInput:
    values = {
        "decision_date": "20260807",
        "buy_date": "20260810",
        "ts_code": "000001.SZ",
        "submitted_qty": 1000,
        "slot_capital_cny": 100000.0,
        "open_price": 10.0,
        "exit_reference_price": 11.0,
        "limit_price": 11.0,
        "price_tick": 0.01,
        "fees": ZERO_FEES,
    }
    values.update(overrides)
    return ExecutionScenarioInput(**values)


class ExecutionScenarioTests(unittest.TestCase):
    def test_shadow_open_is_full_fill_fixed_capital_benchmark(self) -> None:
        result = evaluate_execution_scenario(
            inputs(), shadow_open_benchmark()
        )

        self.assertEqual(result["label_quality"], "SHADOW_OPEN_ASSUMPTION")
        self.assertEqual(result["filled_qty"], 1000)
        self.assertEqual(result["entry_price"], 10.0)
        self.assertEqual(result["exit_price"], 11.0)
        self.assertEqual(result["net_pnl"], 1000.0)
        self.assertAlmostEqual(result["net_return_on_allocated"], 0.01)

    def test_half_fill_keeps_unfilled_capital_as_cash(self) -> None:
        scenario = ExecutionScenario(
            scenario_id="half_fill",
            label="half fill",
            fill_fraction=0.5,
        )
        result = evaluate_execution_scenario(inputs(), scenario)

        self.assertEqual(result["filled_qty"], 500)
        self.assertEqual(result["net_pnl"], 500.0)
        self.assertAlmostEqual(result["net_return_on_allocated"], 0.005)

    def test_zero_fill_has_zero_return_and_no_fees(self) -> None:
        scenario = ExecutionScenario(
            scenario_id="zero_fill",
            label="zero fill",
            fill_fraction=0.0,
        )
        result = evaluate_execution_scenario(inputs(), scenario)

        self.assertEqual(result["filled_qty"], 0)
        self.assertIsNone(result["entry_price"])
        self.assertEqual(result["buy_fees"], 0.0)
        self.assertEqual(result["sell_fees"], 0.0)
        self.assertEqual(result["net_return_on_allocated"], 0.0)

    def test_adverse_prices_are_rounded_to_the_exchange_tick(self) -> None:
        scenario = ExecutionScenario(
            scenario_id="slippage",
            label="slippage",
            entry_slippage_bps=10.0,
            exit_slippage_bps=10.0,
        )
        result = evaluate_execution_scenario(
            inputs(
                submitted_qty=100,
                slot_capital_cny=10000.0,
                exit_reference_price=10.0,
            ),
            scenario,
        )

        self.assertEqual(result["entry_price"], 10.01)
        self.assertEqual(result["exit_price"], 9.99)
        self.assertAlmostEqual(result["net_return_on_allocated"], -0.0002)

    def test_stressed_entry_above_frozen_limit_becomes_zero_fill(self) -> None:
        scenario = ExecutionScenario(
            scenario_id="limit_blocked",
            label="limit blocked",
            entry_slippage_bps=25.0,
        )
        result = evaluate_execution_scenario(
            inputs(open_price=10.99, limit_price=11.0), scenario
        )

        self.assertEqual(result["filled_qty"], 0)
        self.assertEqual(
            result["fill_reason"], "STRESSED_ENTRY_ABOVE_FROZEN_LIMIT"
        )
        self.assertEqual(result["net_return_on_allocated"], 0.0)

    def test_batch_requires_unique_scenario_ids(self) -> None:
        scenario = shadow_open_benchmark()
        with self.assertRaisesRegex(ContractError, "ids must be unique"):
            evaluate_execution_scenarios(inputs(), [scenario, scenario])


if __name__ == "__main__":
    unittest.main()
