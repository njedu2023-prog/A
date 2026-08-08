from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any

from .domain import ContractError, normalize_date, normalize_ts_code


EXECUTION_SCENARIO_SCHEMA_VERSION = "execution_scenarios_v1"
EXECUTION_BASIS = "T_DAILY_UNADJUSTED_OPEN"
RETURN_BASIS = "net_return_on_allocated"


def _finite(value: Any, field_name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{field_name} must be a finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{field_name} must be a finite number") from exc
    if not math.isfinite(parsed) or (positive and parsed <= 0):
        qualifier = "positive " if positive else ""
        raise ContractError(f"{field_name} must be a {qualifier}finite number")
    return parsed


@dataclass(frozen=True)
class FeeSchedule:
    commission_rate: float
    minimum_commission_cny: float
    stamp_duty_sell_rate: float
    transfer_fee_rate_each_side: float

    def __post_init__(self) -> None:
        for field_name, value in asdict(self).items():
            parsed = _finite(value, field_name)
            if parsed < 0:
                raise ContractError(f"{field_name} must be nonnegative")

    @classmethod
    def from_execution_config(cls, execution: dict[str, Any]) -> "FeeSchedule":
        return cls(
            commission_rate=execution["commission_rate"],
            minimum_commission_cny=execution["minimum_commission_cny"],
            stamp_duty_sell_rate=execution["stamp_duty_sell_rate"],
            transfer_fee_rate_each_side=execution[
                "transfer_fee_rate_each_side"
            ],
        )


@dataclass(frozen=True)
class ExecutionScenarioInput:
    decision_date: str
    buy_date: str
    ts_code: str
    submitted_qty: int
    slot_capital_cny: float
    open_price: float
    exit_reference_price: float
    limit_price: float
    price_tick: float
    fees: FeeSchedule

    def __post_init__(self) -> None:
        normalize_date(self.decision_date, "scenario decision_date")
        normalize_date(self.buy_date, "scenario buy_date")
        normalize_ts_code(self.ts_code)
        if (
            isinstance(self.submitted_qty, bool)
            or not isinstance(self.submitted_qty, int)
            or self.submitted_qty <= 0
        ):
            raise ContractError("submitted_qty must be a positive integer")
        for field_name in (
            "slot_capital_cny",
            "open_price",
            "exit_reference_price",
            "limit_price",
            "price_tick",
        ):
            _finite(getattr(self, field_name), field_name, positive=True)


@dataclass(frozen=True)
class ExecutionScenario:
    scenario_id: str
    label: str
    fill_fraction: float = 1.0
    entry_slippage_bps: float = 0.0
    exit_slippage_bps: float = 0.0
    additional_cost_bps: float = 0.0
    label_quality: str = "COUNTERFACTUAL_STRESS"

    def __post_init__(self) -> None:
        if not self.scenario_id.strip() or not self.label.strip():
            raise ContractError("execution scenario requires id and label")
        fill = _finite(self.fill_fraction, "fill_fraction")
        if fill < 0 or fill > 1:
            raise ContractError("fill_fraction must be within [0, 1]")
        for field_name in (
            "entry_slippage_bps",
            "exit_slippage_bps",
            "additional_cost_bps",
        ):
            value = _finite(getattr(self, field_name), field_name)
            if value < 0 or value >= 10_000:
                raise ContractError(f"{field_name} must be within [0, 10000)")
        if self.label_quality not in {
            "SHADOW_OPEN_ASSUMPTION",
            "COUNTERFACTUAL_STRESS",
        }:
            raise ContractError("unsupported execution scenario label_quality")


def shadow_open_benchmark(*, exit_slippage_bps: float = 0.0) -> ExecutionScenario:
    """Return the canonical full-fill T-day open shadow benchmark."""

    return ExecutionScenario(
        scenario_id="shadow_open_full_fill",
        label="开盘价影子全额计入",
        fill_fraction=1.0,
        exit_slippage_bps=exit_slippage_bps,
        label_quality="SHADOW_OPEN_ASSUMPTION",
    )


def standard_pressure_scenarios(
    *,
    base_exit_slippage_bps: float = 0.0,
) -> tuple[ExecutionScenario, ...]:
    """Return transparent stresses, none of which claim actual execution truth."""

    return (
        shadow_open_benchmark(exit_slippage_bps=base_exit_slippage_bps),
        ExecutionScenario(
            scenario_id="queue_half_fill_25bps",
            label="排队半仓成交·买卖各增25bp",
            fill_fraction=0.5,
            entry_slippage_bps=25.0,
            exit_slippage_bps=base_exit_slippage_bps + 25.0,
        ),
        ExecutionScenario(
            scenario_id="queue_zero_fill",
            label="排队未成交",
            fill_fraction=0.0,
        ),
        ExecutionScenario(
            scenario_id="full_fill_50bps_round_trip",
            label="全额成交·买卖各增25bp",
            fill_fraction=1.0,
            entry_slippage_bps=25.0,
            exit_slippage_bps=base_exit_slippage_bps + 25.0,
        ),
    )


def _tick_price(value: float, tick: float, rounding: str) -> float:
    raw_units = Decimal(str(value)) / Decimal(str(tick))
    mode = ROUND_CEILING if rounding == "UP" else ROUND_FLOOR
    units = raw_units.to_integral_value(rounding=mode)
    return float(units * Decimal(str(tick)))


def _commission(amount: float, fees: FeeSchedule) -> float:
    if amount <= 0:
        return 0.0
    return max(fees.minimum_commission_cny, amount * fees.commission_rate)


def _buy_fees(amount: float, fees: FeeSchedule) -> float:
    return _commission(amount, fees) + amount * fees.transfer_fee_rate_each_side


def _sell_fees(amount: float, fees: FeeSchedule) -> float:
    return (
        _commission(amount, fees)
        + amount * fees.transfer_fee_rate_each_side
        + amount * fees.stamp_duty_sell_rate
    )


def evaluate_execution_scenario(
    inputs: ExecutionScenarioInput,
    scenario: ExecutionScenario,
) -> dict[str, Any]:
    """Evaluate one scenario on a fixed-capital slot using explicit assumptions."""

    entry_price = _tick_price(
        inputs.open_price * (1.0 + scenario.entry_slippage_bps / 10_000.0),
        inputs.price_tick,
        "UP",
    )
    exit_price = _tick_price(
        inputs.exit_reference_price
        * (1.0 - scenario.exit_slippage_bps / 10_000.0),
        inputs.price_tick,
        "DOWN",
    )
    desired_qty = math.floor(inputs.submitted_qty * scenario.fill_fraction)
    fill_reason = "SCENARIO_FILL_FRACTION"
    if entry_price > inputs.limit_price + inputs.price_tick / 2.0:
        filled_qty = 0
        fill_reason = "STRESSED_ENTRY_ABOVE_FROZEN_LIMIT"
    else:
        filled_qty = desired_qty
    if filled_qty == 0 and fill_reason == "SCENARIO_FILL_FRACTION":
        fill_reason = "SCENARIO_ZERO_FILL"

    buy_amount = entry_price * filled_qty
    sell_amount = exit_price * filled_qty
    buy_fees = _buy_fees(buy_amount, inputs.fees)
    sell_fees = _sell_fees(sell_amount, inputs.fees)
    additional_cost = (
        buy_amount * scenario.additional_cost_bps / 10_000.0
    )
    net_pnl = sell_amount - sell_fees - buy_amount - buy_fees - additional_cost
    return {
        "scenario_id": scenario.scenario_id,
        "label": scenario.label,
        "execution_basis": EXECUTION_BASIS,
        "label_quality": scenario.label_quality,
        "return_basis": RETURN_BASIS,
        "assumptions": {
            "fill_fraction": scenario.fill_fraction,
            "entry_slippage_bps": scenario.entry_slippage_bps,
            "exit_slippage_bps": scenario.exit_slippage_bps,
            "additional_cost_bps": scenario.additional_cost_bps,
            "quantity_rounding": "FLOOR_TO_WHOLE_SHARE",
        },
        "submitted_qty": inputs.submitted_qty,
        "desired_filled_qty": desired_qty,
        "filled_qty": filled_qty,
        "actual_fill_fraction": filled_qty / inputs.submitted_qty,
        "fill_reason": fill_reason,
        "entry_price": entry_price if filled_qty else None,
        "exit_price": exit_price if filled_qty else None,
        "buy_amount": buy_amount,
        "sell_amount": sell_amount,
        "buy_fees": buy_fees,
        "sell_fees": sell_fees,
        "additional_cost": additional_cost,
        "net_pnl": net_pnl,
        RETURN_BASIS: net_pnl / inputs.slot_capital_cny,
    }


def evaluate_execution_scenarios(
    inputs: ExecutionScenarioInput,
    scenarios: tuple[ExecutionScenario, ...] | list[ExecutionScenario],
) -> dict[str, Any]:
    if not scenarios:
        raise ContractError("at least one execution scenario is required")
    ids = [scenario.scenario_id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise ContractError("execution scenario ids must be unique")
    return {
        "schema_version": EXECUTION_SCENARIO_SCHEMA_VERSION,
        "decision_date": normalize_date(inputs.decision_date, "decision_date"),
        "buy_date": normalize_date(inputs.buy_date, "buy_date"),
        "ts_code": normalize_ts_code(inputs.ts_code),
        "return_unit": "decimal",
        "return_basis": RETURN_BASIS,
        "slot_capital_cny": inputs.slot_capital_cny,
        "scenarios": [
            evaluate_execution_scenario(inputs, scenario)
            for scenario in scenarios
        ],
    }
