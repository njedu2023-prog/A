from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .calendar import TradingCalendar, parse_calendar_date
from .domain import AuctionTruth, ContractError, normalize_date
from .market import daily_bar_on


STATE_SCHEMA = "three_table_state_v1"


def empty_state() -> dict[str, Any]:
    return {"schema_version": STATE_SCHEMA, "signals": [], "trades": []}


def load_json(path: str | Path, default: Any) -> Any:
    file_path = Path(path)
    if not file_path.exists():
        return default
    with file_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: str | Path, payload: Any) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = file_path.with_suffix(file_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")
    temporary.replace(file_path)


def load_state(path: str | Path) -> dict[str, Any]:
    state = load_json(path, empty_state())
    if state.get("schema_version") != STATE_SCHEMA:
        raise ValueError(f"unsupported state schema: {state.get('schema_version')}")
    state.setdefault("signals", [])
    state.setdefault("trades", [])
    for trade in state["trades"]:
        _migrate_trade_execution(trade)
    return state


def add_signal(state: dict[str, Any], signal: dict[str, Any]) -> bool:
    decision_date = signal["decision_date"]
    if any(item.get("decision_date") == decision_date for item in state["signals"]):
        return False
    state["signals"].append(signal)
    state["signals"].sort(key=lambda item: item["decision_date"])
    return True


def migrate_signal_candidates_to_shadow(signal: dict[str, Any]) -> bool:
    """Migrate every frozen intersection candidate into shadow-only mode.

    Older frozen signals used ``NO_TRADE`` for ranks outside the fixed 1/2/3
    reporting slots.  That policy decision remains auditable, but it must not
    prevent an otherwise valid intersection candidate from collecting an
    execution label in the shadow ledger.
    """

    changed = False
    for candidate in signal.get("candidates", []):
        metrics = candidate.setdefault("metrics", {})
        eligible = bool(metrics.get("policy_trade_eligible", False))
        previous_action = candidate.get("action")
        previous_reason = candidate.get("action_reason")
        candidate["policy_decision"] = {
            "trade_eligible": eligible,
            "broker_order_created": False,
        }
        if previous_action == "SHADOW":
            continue
        audit = candidate.setdefault("action_audit", [])
        previous = {
            "action": previous_action,
            "action_reason": previous_reason,
        }
        if previous not in audit:
            audit.append(previous)
        candidate["action"] = "SHADOW"
        candidate["action_reason"] = (
            "shadow_validation_all_intersection_candidates;"
            f"policy_gate={'TRADE' if eligible else 'NO_TRADE'};"
            "not_a_broker_order;previous_action_preserved_in_action_audit"
        )
        changed = True
    return changed


def ensure_shadow_trades(
    state: dict[str, Any],
    signal: dict[str, Any],
    tracked_ranks: list[int] | None = None,
) -> None:
    """Idempotently create one shadow trade for every ranked candidate.

    ``tracked_ranks`` remains accepted for compatibility with callers, but it
    controls reporting slots only.  It must never filter the all-candidate
    execution ledger.
    """

    del tracked_ranks
    existing_by_id = {trade["trade_id"]: trade for trade in state["trades"]}
    for candidate in signal.get("candidates", []):
        rank = candidate.get("rank")
        trade_id = f"{signal['decision_date']}:R{rank}"
        if trade_id in existing_by_id:
            existing = existing_by_id[trade_id]
            if (
                existing.get("signal_id") != signal.get("signal_id")
                or existing.get("ts_code") != candidate.get("ts_code")
                or existing.get("rank") != rank
            ):
                raise ValueError(f"shadow trade identity mismatch for {trade_id}")
            existing["policy_trade_eligible"] = candidate.get("metrics", {}).get(
                "policy_trade_eligible",
                False,
            )
            existing["execution_mode"] = "SHADOW_ONLY"
            continue
        trade = {
            "trade_id": trade_id,
            "signal_id": signal["signal_id"],
            "decision_date": signal["decision_date"],
            "buy_date": signal["buy_date"],
            "planned_exit_date": signal["exit_date"],
            "rank": rank,
            "ts_code": candidate["ts_code"],
            "name": candidate["name"],
            "model_score": candidate.get("metrics", {}).get("utility_score"),
            "policy_trade_eligible": candidate.get("metrics", {}).get(
                "policy_trade_eligible",
                False,
            ),
            "execution_mode": "SHADOW_ONLY",
            "status": "PENDING_BUY",
            "reason": "waiting_for_exact_auction_truth",
            "buy": None,
            "exit": None,
            "pnl": None,
            "diagnostics": {},
        }
        state["trades"].append(trade)
        existing_by_id[trade_id] = trade
    state["trades"].sort(key=lambda item: (item["decision_date"], item["rank"]))


def _commission(amount: float, execution: dict[str, Any]) -> float:
    if amount <= 0:
        return 0.0
    return max(float(execution["minimum_commission_cny"]), amount * float(execution["commission_rate"]))


def _buy_fees(amount: float, execution: dict[str, Any]) -> float:
    return _commission(amount, execution) + amount * float(execution["transfer_fee_rate_each_side"])


def _sell_fill_fees(amount: float, execution: dict[str, Any]) -> float:
    if amount <= 0:
        return 0.0
    return (
        _commission(amount, execution)
        + amount * float(execution["transfer_fee_rate_each_side"])
        + amount * float(execution["stamp_duty_sell_rate"])
    )


def _truth_key(date: str, ts_code: str) -> str:
    return f"{date}:{ts_code}"


def _volume_shares(bar: Any, lot_size: int) -> float | None:
    volume = float(bar.volume)
    if not math.isfinite(volume) or volume <= 0:
        return None
    unit = str(getattr(bar, "volume_unit", "UNSPECIFIED")).upper()
    if unit == "LOT":
        return volume * lot_size
    if unit == "SHARE":
        return volume
    return None


def _minute_vwap(bar: Any, lot_size: int) -> float | None:
    shares = _volume_shares(bar, lot_size)
    if float(bar.volume) <= 0 or float(bar.amount) <= 0:
        return None
    if shares is None:
        return None
    value = float(bar.amount) / shares
    tick = float(getattr(bar, "price_tick", 0.01) or 0.01)
    if not math.isfinite(value) or value < float(bar.low) - tick / 2.0 or value > float(bar.high) + tick / 2.0:
        return None
    return value


def _window_minutes(execution: dict[str, Any]) -> list[str]:
    start_text = str(execution["exit_window_start"])
    end_text = str(execution["exit_window_end"])
    try:
        start = datetime.strptime(start_text, "%H:%M")
        end = datetime.strptime(end_text, "%H:%M")
    except ValueError as exc:
        raise ContractError("exit window must use HH:MM") from exc
    count = int((end - start).total_seconds() // 60)
    expected_count = int(execution["exit_minute_count"])
    if count <= 0 or count != expected_count:
        raise ContractError("exit window duration does not match exit_minute_count")
    return [
        (start + timedelta(minutes=index)).strftime("%H:%M")
        for index in range(count)
    ]


def _window_targets(quantity: int, minute_count: int, lot_size: int) -> list[int]:
    if quantity <= 0:
        return [0] * minute_count
    full_lots, odd_lot = divmod(quantity, lot_size)
    targets = [(full_lots // minute_count) * lot_size for _ in range(minute_count)]
    for index in range(full_lots % minute_count):
        targets[index] += lot_size
    if odd_lot:
        targets[-1] += odd_lot
    return targets


def _price_decimals(tick: float) -> int:
    text = f"{tick:.10f}".rstrip("0")
    return len(text.split(".", 1)[1]) if "." in text else 0


def _legal_sell_price(bar: Any, benchmark: float, execution: dict[str, Any]) -> float | None:
    tick = float(getattr(bar, "price_tick", None) or execution.get("price_tick", 0.01))
    low = float(bar.low)
    high = float(bar.high)
    if tick <= 0 or not all(math.isfinite(item) for item in (low, high)) or low <= 0 or high < low:
        return None
    limit_down = getattr(bar, "limit_down", None)
    lower_bound = low
    if limit_down is not None:
        limit_value = float(limit_down)
        if not math.isfinite(limit_value) or limit_value <= 0:
            return None
        lower_bound = max(lower_bound, limit_value)
    raw = benchmark * (1.0 - float(execution["slippage_rate_each_side"]))
    bounded = min(high, max(lower_bound, raw))
    units = math.floor((bounded + 1e-12) / tick)
    price = units * tick
    if price < lower_bound - tick / 10.0:
        price = math.ceil((lower_bound - 1e-12) / tick) * tick
    price = round(price, _price_decimals(tick))
    if price < lower_bound - tick / 10.0 or price > high + tick / 10.0:
        return None
    return price


def _is_locked_limit_down(bar: Any, execution: dict[str, Any]) -> bool:
    tick = float(getattr(bar, "price_tick", None) or execution.get("price_tick", 0.01))
    low = float(bar.low)
    high = float(bar.high)
    limit_down = getattr(bar, "limit_down", None)
    if limit_down is not None:
        limit_value = float(limit_down)
        return high <= limit_value + tick / 2.0 and low <= limit_value + tick / 2.0
    if bool(execution.get("one_price_bar_is_locked_without_limit", True)):
        return abs(high - low) < tick / 2.0
    return False


def _record_attempt(exit_payload: dict[str, Any], attempt: dict[str, Any]) -> None:
    attempts = exit_payload.setdefault("attempts", [])
    existing = next((item for item in attempts if item.get("date") == attempt["date"]), None)
    if existing is None:
        attempt["attempt_count"] = 1
        attempts.append(attempt)
    else:
        attempt["attempt_count"] = int(existing.get("attempt_count", 1)) + 1
        existing.clear()
        existing.update(attempt)


def _migrate_trade_execution(trade: dict[str, Any], execution: dict[str, Any] | None = None) -> None:
    """Add execution-v2 fields without inventing fills for legacy state."""

    trade.setdefault("diagnostics", {})
    exit_payload = trade.get("exit")
    if not isinstance(exit_payload, dict):
        return
    fills = exit_payload.setdefault("fills", [])
    for fill in fills:
        fill.setdefault("date", trade.get("planned_exit_date"))
        if fill.get("date") and fill.get("minute"):
            fill.setdefault("fill_id", f"{fill['date']}T{fill['minute']}")
            fill.setdefault("at", f"{fill['date']}T{fill['minute']}:00+08:00")
        if "amount" not in fill and fill.get("qty") is not None and fill.get("price") is not None:
            fill["amount"] = float(fill["qty"]) * float(fill["price"])
        if trade.get("status") != "CLOSED" and "fees" not in fill and execution is not None:
            fill["fees"] = _sell_fill_fees(float(fill.get("amount") or 0.0), execution)
    exit_payload.setdefault("attempts", [])
    processed = exit_payload.setdefault("processed_windows", [])
    if (
        trade.get("status") in {"EXIT_DELAYED", "CLOSED"}
        and trade.get("planned_exit_date")
        and (fills or exit_payload.get("benchmark_twap") is not None)
        and trade["planned_exit_date"] not in processed
    ):
        processed.append(trade["planned_exit_date"])
    exit_payload.setdefault("benchmark_twaps", [])
    if trade.get("status") != "CLOSED":
        filled_qty = sum(int(item.get("qty") or 0) for item in fills)
        sell_amount = sum(float(item.get("amount") or 0.0) for item in fills)
        fees = sum(float(item.get("fees") or 0.0) for item in fills)
        bought_qty = int((trade.get("buy") or {}).get("filled_qty") or 0)
        exit_payload["filled_qty"] = filled_qty
        exit_payload["remaining_qty"] = max(0, bought_qty - filled_qty)
        exit_payload["sell_amount"] = sell_amount
        exit_payload["fees"] = fees
        exit_payload["net_proceeds"] = sell_amount - fees
    exit_payload.setdefault("actual_exit_at", None)
    exit_payload.setdefault("actual_exit_date", None)
    exit_payload.setdefault("time_precision", "minute")
    exit_payload.setdefault("label_quality", "CONSERVATIVE")
    exit_payload.setdefault("data_tier", "MINUTE_BAR")


def _ensure_exit_payload(trade: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
    _migrate_trade_execution(trade, execution)
    if isinstance(trade.get("exit"), dict):
        return trade["exit"]
    start = str(execution["exit_window_start"])
    end = str(execution["exit_window_end"])
    payload = {
        "target_start": f"{trade['planned_exit_date']}T{start}:00+08:00",
        "target_end": f"{trade['planned_exit_date']}T{end}:00+08:00",
        "benchmark_twap": None,
        "benchmark_twaps": [],
        "fills": [],
        "filled_qty": 0,
        "remaining_qty": int(trade["buy"]["filled_qty"]),
        "sell_amount": 0.0,
        "fees": 0.0,
        "net_proceeds": 0.0,
        "delay_trading_days": None,
        "processed_windows": [],
        "attempts": [],
        "actual_exit_at": None,
        "actual_exit_date": None,
        "time_precision": "minute",
        "label_quality": "CONSERVATIVE",
        "data_tier": "MINUTE_BAR",
    }
    trade["exit"] = payload
    return payload


def _settle_entry(
    trade: dict[str, Any],
    truth: dict[str, Any],
    provider: Any,
    execution: dict[str, Any],
) -> None:
    key = _truth_key(trade["buy_date"], trade["ts_code"])
    record = (truth.get("auctions") or {}).get(key)
    if record is not None:
        if not isinstance(record, dict):
            trade["status"] = "BUY_UNVERIFIABLE"
            trade["reason"] = "auction_truth_record_must_be_an_object"
            return
        try:
            auction = AuctionTruth.from_record(
                record,
                expected_date=trade["buy_date"],
                expected_ts_code=trade["ts_code"],
                execution=execution,
            )
        except ContractError as exc:
            trade["status"] = "BUY_UNVERIFIABLE"
            trade["reason"] = "auction_truth_contract_failed"
            trade["diagnostics"]["auction_truth_error"] = str(exc)
            return
        trade["diagnostics"].pop("auction_truth_error", None)
        if auction.participation_cap_breached:
            trade["diagnostics"]["auction_participation_cap_breached"] = {
                "rate": auction.participation_rate,
                "cap": float(execution["auction_participation_cap"]),
                "accepted_as_actual_broker_fact": auction.label_quality == "ACTUAL",
            }
        if auction.filled_qty == 0:
            trade["status"] = "BUY_UNFILLED"
            trade["reason"] = auction.reason or "auction_not_filled"
            trade["buy"] = {
                "at": auction.event_at,
                "submitted_qty": auction.submitted_qty,
                "filled_qty": 0,
                "avg_price": None,
                "amount": 0.0,
                "fees": 0.0,
                "limit_price": auction.limit_price,
                "auction_matched_qty": auction.auction_matched_qty,
                "queue_ahead_qty": auction.queue_ahead_qty,
                "executable_qty_at_order": auction.executable_qty_at_order,
                "participation_rate": auction.participation_rate,
                "source": auction.source,
                "data_tier": auction.data_tier,
                "label_quality": auction.label_quality,
                "price_limit_source": auction.price_limit_source,
            }
            return
        price = float(auction.price)
        amount = price * auction.filled_qty
        fees = _buy_fees(amount, execution)
        trade["status"] = "OPEN"
        trade["reason"] = "waiting_for_t1_exit"
        trade["buy"] = {
            "at": auction.event_at,
            "submitted_qty": auction.submitted_qty,
            "filled_qty": auction.filled_qty,
            "avg_price": price,
            "amount": amount,
            "fees": fees,
            "limit_price": auction.limit_price,
            "auction_matched_qty": auction.auction_matched_qty,
            "queue_ahead_qty": auction.queue_ahead_qty,
            "executable_qty_at_order": auction.executable_qty_at_order,
            "participation_rate": auction.participation_rate,
            "source": auction.source,
            "data_tier": auction.data_tier,
            "label_quality": auction.label_quality,
            "price_limit_source": auction.price_limit_source,
        }
        return

    try:
        proxy = daily_bar_on(provider, trade["ts_code"], trade["buy_date"])
    except Exception as exc:
        trade["diagnostics"]["auction_proxy_error"] = str(exc)
        return
    if proxy is not None:
        trade["diagnostics"]["daily_open_proxy"] = {
            "price": proxy.open,
            "date": proxy.date,
            "provider": getattr(proxy, "provider", "UNSPECIFIED"),
            "price_adjustment": getattr(proxy, "price_adjustment", "UNSPECIFIED"),
            "counts_as_fill": False,
            "reason": "daily open may be the first continuous-auction trade when opening auction has no clearing price",
        }
        trade["status"] = "BUY_UNVERIFIABLE"
        trade["reason"] = "exact_0925_auction_fill_required"


def _close_trade(trade: dict[str, Any], execution: dict[str, Any], attempt_date: str, delay_days: int) -> None:
    exit_payload = trade["exit"]
    fills = exit_payload["fills"]
    if int(exit_payload["remaining_qty"]) != 0 or not fills:
        raise ContractError("cannot close a trade with unsold shares")
    sold_amount = sum(float(item["amount"]) for item in fills)
    sell_fees = sum(float(item["fees"]) for item in fills)
    sold_qty = sum(int(item["qty"]) for item in fills)
    last_fill = max(fills, key=lambda item: item["at"])
    exit_payload.update(
        {
            "filled_qty": sold_qty,
            "remaining_qty": 0,
            "sell_amount": sold_amount,
            "fees": sell_fees,
            "net_proceeds": sold_amount - sell_fees,
            "avg_price": sold_amount / sold_qty,
            "delay_trading_days": delay_days,
            "actual_exit_at": last_fill["at"],
            "actual_exit_date": attempt_date,
            "time_precision": "minute",
        }
    )
    buy_total = float(trade["buy"]["amount"]) + float(trade["buy"]["fees"])
    net_pnl = sold_amount - sell_fees - buy_total
    allocated_capital = float(execution["slot_capital_cny"])
    trade["status"] = "CLOSED"
    trade["reason"] = "shadow_exit_completed"
    trade["pnl"] = {
        "state": "REALIZED",
        "gross_return": sold_amount / float(trade["buy"]["amount"]) - 1.0,
        "net_return_on_invested": net_pnl / buy_total,
        "net_return_on_allocated": net_pnl / allocated_capital,
        "net_pnl": net_pnl,
        "asof": last_fill["at"],
    }


def _settle_exit_window(
    trade: dict[str, Any],
    provider: Any,
    execution: dict[str, Any],
    attempt_date: str,
    delay_days: int,
) -> bool:
    exit_payload = _ensure_exit_payload(trade, execution)
    if attempt_date in exit_payload["processed_windows"]:
        return True
    wanted = _window_minutes(execution)
    try:
        bars = provider.minute_bars(trade["ts_code"], attempt_date)
    except Exception as exc:
        trade["status"] = "EXIT_UNVERIFIABLE"
        trade["reason"] = "exit_market_data_unavailable"
        trade["diagnostics"]["exit_market_error"] = str(exc)
        _record_attempt(
            exit_payload,
            {"date": attempt_date, "result": "UNVERIFIABLE", "reason": trade["reason"]},
        )
        return False
    selected = [bar for bar in bars if bar.time in wanted]
    by_time = {bar.time: bar for bar in selected}
    duplicate_times = sorted({bar.time for bar in selected if sum(item.time == bar.time for item in selected) > 1})
    invalid_semantics = sorted(
        {
            str(bar.time)
            for bar in selected
            if str(getattr(bar, "time_semantics", "")).upper() != "INTERVAL_START"
        }
    )
    invalid_adjustments = sorted(
        {
            str(bar.time)
            for bar in selected
            if str(getattr(bar, "price_adjustment", "")).upper() != "NONE"
        }
    )
    if (
        any(minute not in by_time for minute in wanted)
        or duplicate_times
        or invalid_semantics
        or invalid_adjustments
    ):
        trade["status"] = "EXIT_UNVERIFIABLE"
        trade["reason"] = "five_complete_exit_minutes_required"
        trade["diagnostics"]["available_exit_minutes"] = sorted(by_time)
        _record_attempt(
            exit_payload,
            {
                "date": attempt_date,
                "result": "UNVERIFIABLE",
                "reason": trade["reason"],
                "available_minutes": sorted(by_time),
                "duplicate_minutes": duplicate_times,
                "invalid_time_semantics": invalid_semantics,
                "invalid_price_adjustments": invalid_adjustments,
            },
        )
        return False
    lot = int(execution["lot_size"])
    minute_prices: list[float] = []
    for minute in wanted:
        value = _minute_vwap(by_time[minute], lot)
        if value is None:
            trade["status"] = "EXIT_UNVERIFIABLE"
            trade["reason"] = "minute_vwap_requires_amount_volume_and_declared_units"
            _record_attempt(
                exit_payload,
                {
                    "date": attempt_date,
                    "result": "UNVERIFIABLE",
                    "reason": trade["reason"],
                    "minute": minute,
                },
            )
            return False
        minute_prices.append(value)

    remaining = int(exit_payload["remaining_qty"])
    targets = _window_targets(remaining, len(wanted), lot)
    participation = float(execution["max_exit_participation_rate"])
    if not 0 < participation <= 1:
        raise ContractError("max_exit_participation_rate must be in (0, 1]")
    locked_minutes: list[str] = []
    new_fill_ids: list[str] = []
    pending_fills: list[dict[str, Any]] = []
    existing_fill_ids = {item.get("fill_id") for item in exit_payload["fills"]}
    for minute, target, price in zip(wanted, targets, minute_prices, strict=True):
        if target <= 0 or remaining <= 0:
            continue
        bar = by_time[minute]
        if _is_locked_limit_down(bar, execution):
            locked_minutes.append(minute)
            continue
        volume_shares = _volume_shares(bar, lot)
        if volume_shares is None:
            trade["status"] = "EXIT_UNVERIFIABLE"
            trade["reason"] = "minute_volume_unit_unverifiable"
            _record_attempt(
                exit_payload,
                {
                    "date": attempt_date,
                    "result": "UNVERIFIABLE",
                    "reason": trade["reason"],
                    "minute": minute,
                },
            )
            return False
        raw_capacity = math.floor(volume_shares * participation)
        capacity = math.floor(raw_capacity / lot) * lot if target >= lot else raw_capacity
        quantity = min(target, capacity, remaining)
        if quantity <= 0:
            continue
        execution_price = _legal_sell_price(bar, price, execution)
        if execution_price is None:
            trade["status"] = "EXIT_UNVERIFIABLE"
            trade["reason"] = "legal_execution_price_unverifiable"
            _record_attempt(
                exit_payload,
                {
                    "date": attempt_date,
                    "result": "UNVERIFIABLE",
                    "reason": trade["reason"],
                    "minute": minute,
                },
            )
            return False
        fill_id = f"{attempt_date}T{minute}"
        if fill_id in existing_fill_ids:
            continue
        amount = quantity * execution_price
        pending_fills.append(
            {
                "fill_id": fill_id,
                "at": f"{attempt_date}T{minute}:00+08:00",
                "date": attempt_date,
                "minute": minute,
                "qty": quantity,
                "benchmark_price": price,
                "price": execution_price,
                "amount": amount,
                "fees": _sell_fill_fees(amount, execution),
                "data_tier": "MINUTE_BAR",
                "label_quality": "CONSERVATIVE",
                "market_data_provider": getattr(bar, "provider", "UNSPECIFIED"),
                "price_adjustment": getattr(bar, "price_adjustment", "UNSPECIFIED"),
            }
        )
        new_fill_ids.append(fill_id)
        remaining -= quantity

    exit_payload["fills"].extend(pending_fills)
    benchmark_twap = sum(minute_prices) / len(minute_prices)
    if exit_payload.get("benchmark_twap") is None and attempt_date == trade["planned_exit_date"]:
        exit_payload["benchmark_twap"] = benchmark_twap
    exit_payload["benchmark_twaps"] = [
        item for item in exit_payload["benchmark_twaps"] if item.get("date") != attempt_date
    ]
    exit_payload["benchmark_twaps"].append({"date": attempt_date, "value": benchmark_twap})
    exit_payload["processed_windows"].append(attempt_date)
    fills = exit_payload["fills"]
    sold_qty = sum(int(item["qty"]) for item in fills)
    sold_amount = sum(float(item["amount"]) for item in fills)
    sell_fees = sum(float(item["fees"]) for item in fills)
    exit_payload.update(
        {
            "filled_qty": sold_qty,
            "remaining_qty": remaining,
            "sell_amount": sold_amount,
            "fees": sell_fees,
            "net_proceeds": sold_amount - sell_fees,
            "avg_price": sold_amount / sold_qty if sold_qty else None,
        }
    )
    _record_attempt(
        exit_payload,
        {
            "date": attempt_date,
            "result": "FILLED" if remaining == 0 else "DELAYED",
            "reason": "completed" if remaining == 0 else "locked_or_capacity_insufficient",
            "benchmark_twap": benchmark_twap,
            "new_fill_ids": new_fill_ids,
            "locked_minutes": locked_minutes,
            "remaining_qty": remaining,
            "delay_trading_days": delay_days,
        },
    )
    if remaining > 0:
        trade["status"] = "EXIT_DELAYED"
        trade["reason"] = "exit_window_locked_or_capacity_insufficient"
        trade["pnl"] = None
    else:
        _close_trade(trade, execution, attempt_date, delay_days)
    return True


def _exit_dates_through(
    planned_exit_date: str,
    asof_date: str,
    calendar: TradingCalendar,
) -> list[str]:
    start = parse_calendar_date(planned_exit_date, "planned_exit_date")
    end = parse_calendar_date(asof_date, "asof_date")
    if start > end:
        return []
    if not calendar.is_open(start, "planned_exit_date"):
        raise ContractError(f"planned_exit_date={planned_exit_date} is not a trading day")
    calendar.is_open(end, "asof_date")  # validates the supported calendar year
    dates: list[str] = []
    cursor = start
    while cursor <= end:
        dates.append(cursor.strftime("%Y%m%d"))
        if cursor == end:
            break
        next_date = calendar.next_open_date(cursor, "delayed_exit_date")
        if next_date > end:
            break
        cursor = next_date
    return dates


def settle_trades(
    state: dict[str, Any],
    truth: dict[str, Any],
    provider: Any,
    execution: dict[str, Any],
    asof_date: str | None = None,
) -> None:
    today = normalize_date(
        asof_date or datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d"),
        "asof_date",
    )
    calendar_path = execution.get("trading_calendar_path")
    calendar = TradingCalendar.from_file(calendar_path) if calendar_path else TradingCalendar.from_file()
    for trade in state["trades"]:
        _migrate_trade_execution(trade, execution)
        if trade["status"] in {"PENDING_BUY", "BUY_UNVERIFIABLE"} and trade["buy_date"] <= today:
            _settle_entry(trade, truth, provider, execution)
        if (
            trade["status"] in {"OPEN", "EXIT_DELAYED", "EXIT_UNVERIFIABLE"}
            and trade.get("buy")
            and trade["planned_exit_date"] <= today
        ):
            exit_payload = _ensure_exit_payload(trade, execution)
            for delay_days, attempt_date in enumerate(
                _exit_dates_through(trade["planned_exit_date"], today, calendar)
            ):
                if attempt_date in exit_payload["processed_windows"]:
                    continue
                verified = _settle_exit_window(
                    trade,
                    provider,
                    execution,
                    attempt_date,
                    delay_days,
                )
                if not verified or trade["status"] == "CLOSED":
                    break
