from __future__ import annotations

import math
from typing import Any

from .candidate_facts import candidate_validation_inputs
from .domain import Candidate, ContractError, normalize_date


ORDER_SPEC_SCHEMA = "shadow_order_spec_v1"


def _reserved_cash(quantity: int, limit_price: float, execution: dict[str, Any]) -> float:
    amount = quantity * limit_price
    commission = max(
        float(execution["minimum_commission_cny"]),
        amount * float(execution["commission_rate"]),
    )
    transfer = amount * float(execution["transfer_fee_rate_each_side"])
    return amount + commission + transfer


def maximum_lot_quantity(limit_price: float, execution: dict[str, Any]) -> int:
    capital = float(execution["slot_capital_cny"])
    lot = int(execution["lot_size"])
    if not math.isfinite(limit_price) or limit_price <= 0:
        raise ContractError("order specification requires a positive limit price")
    if not math.isfinite(capital) or capital <= 0 or lot <= 0:
        raise ContractError("order specification has invalid capital or lot size")
    quantity = int(capital // limit_price // lot) * lot
    while quantity > 0 and _reserved_cash(quantity, limit_price, execution) > capital + 1e-8:
        quantity -= lot
    if quantity <= 0:
        raise ContractError("slot capital cannot fund one board lot at the frozen limit price")
    return quantity


def build_order_spec(
    candidate: Candidate,
    *,
    decision_date: str,
    buy_date: str,
    execution: dict[str, Any],
) -> dict[str, Any]:
    """Freeze a comparable 09:25 shadow order before any T-day evidence exists."""

    facts = candidate_validation_inputs(candidate)
    limit_price = facts.get("limit_up_price")
    if limit_price is None:
        raise ContractError("candidate has no frozen exchange limit price")
    price = float(limit_price)
    quantity = maximum_lot_quantity(price, execution)
    return {
        "schema_version": ORDER_SPEC_SCHEMA,
        "decision_date": normalize_date(decision_date, "order decision_date"),
        "trade_date": normalize_date(buy_date, "order trade_date"),
        "event_time": str(execution.get("auction_time", "09:25")),
        "phase": str(
            execution.get("auction_phase", "OPENING_CALL_AUCTION")
        ).upper(),
        "side": "BUY",
        "order_type": "LIMIT",
        "limit_price_policy": "FROZEN_D_LIMIT_UP_MARKETABLE_LIMIT",
        "limit_price": price,
        "price_limit_source": facts.get("limit_up_source"),
        "submitted_qty": quantity,
        "quantity_unit": "SHARES",
        "lot_size": int(execution["lot_size"]),
        "slot_capital_cny": float(execution["slot_capital_cny"]),
        "maximum_reserved_cash_cny": _reserved_cash(
            quantity,
            price,
            execution,
        ),
        "execution_mode": "SHADOW_ONLY",
    }


def validate_truth_against_order_spec(
    order_spec: Any,
    *,
    submitted_qty: int,
    limit_price: float,
    price_tick: float,
) -> None:
    if not isinstance(order_spec, dict):
        return
    if order_spec.get("schema_version") != ORDER_SPEC_SCHEMA:
        raise ContractError("unsupported frozen order specification")
    expected_qty = order_spec.get("submitted_qty")
    if (
        isinstance(expected_qty, bool)
        or not isinstance(expected_qty, int)
        or expected_qty <= 0
        or submitted_qty != expected_qty
    ):
        raise ContractError(
            "auction truth submitted_qty does not match the frozen order specification"
        )
    try:
        expected_price = float(order_spec["limit_price"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("frozen order specification has an invalid limit price") from exc
    if not math.isfinite(expected_price) or abs(limit_price - expected_price) > price_tick / 2.0:
        raise ContractError(
            "auction truth limit_price does not match the frozen order specification"
        )


__all__ = [
    "ORDER_SPEC_SCHEMA",
    "build_order_spec",
    "maximum_lot_quantity",
    "validate_truth_against_order_spec",
]
