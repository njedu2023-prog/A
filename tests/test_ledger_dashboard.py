from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from three_table_quant.dashboard import build_dashboard, validate_dashboard
from three_table_quant.ledger import (
    empty_state,
    ensure_shadow_trades,
    migrate_signal_candidates_to_shadow,
    settle_trades,
)
from three_table_quant.market import Bar, EastmoneyMarketData, period_end_to_interval_start
from three_table_quant.pipeline import _ensure_all_candidate_shadow_ledger


EXECUTION = {
    "slot_capital_cny": 100000.0,
    "lot_size": 100,
    "price_tick": .01,
    "auction_time": "09:25",
    "auction_phase": "OPENING_CALL_AUCTION",
    "auction_truth_schema": "auction_execution_v2",
    "auction_participation_cap": .01,
    "accepted_price_limit_sources": ["EXCHANGE_SECURITY_MASTER", "BROKER_ORDER_RECORD"],
    "reliable_zero_fill_reasons": ["NO_AUCTION_MATCH", "SUSPENDED", "ORDER_REJECTED", "QUEUE_NOT_REACHED", "INSUFFICIENT_SELL_VOLUME"],
    "exit_window_start": "11:00",
    "exit_window_end": "11:05",
    "exit_minute_count": 5,
    "commission_rate": .0003,
    "minimum_commission_cny": 5.0,
    "stamp_duty_sell_rate": .0005,
    "transfer_fee_rate_each_side": .00001,
    "slippage_rate_each_side": .0005,
    "max_exit_participation_rate": .05,
    "one_price_bar_is_locked_without_limit": True,
    "trading_calendar_path": "data/trading_calendar_2026.json",
}
OPEN_FILL_EXECUTION = {
    **EXECUTION,
    "daily_open_counts_as_fill": True,
}


class FakeMarket:
    def __init__(
        self,
        complete: bool = True,
        *,
        incomplete_dates: set[str] | None = None,
        locked_dates: set[str] | None = None,
        volume_lots_by_date: dict[str, float] | None = None,
    ) -> None:
        self.complete = complete
        self.incomplete_dates = set(incomplete_dates or ())
        self.locked_dates = set(locked_dates or ())
        self.volume_lots_by_date = dict(volume_lots_by_date or {})

    def daily_bars(self, code: str, end_date: str, limit: int = 15) -> list[Bar]:
        return [Bar(end_date, None, 10, 10, 10.2, 9.8, 1000, 1000000)]

    def minute_bars(self, code: str, trade_date: str) -> list[Bar]:
        times = ["11:00", "11:01", "11:02", "11:03", "11:04"]
        if not self.complete or trade_date in self.incomplete_dates:
            times.pop()
        locked = trade_date in self.locked_dates
        price = 9.0 if locked else 10.1
        high = price if locked else 10.2
        low = price if locked else 10.0
        volume_lots = self.volume_lots_by_date.get(trade_date, 10000)
        amount = price * volume_lots * 100.0
        return [
            Bar(
                trade_date,
                minute,
                price,
                price,
                high,
                low,
                volume_lots,
                amount,
                volume_unit="LOT",
                limit_down=price if locked else 9.0,
                time_semantics="INTERVAL_START",
                provider="TEST",
                price_adjustment="NONE",
            )
            for minute in times
        ]


class OnePriceNoLimitMarket(FakeMarket):
    def __init__(self, price: float, *, limit_down: float | None = None) -> None:
        super().__init__()
        self.price = price
        self.limit_down = limit_down

    def minute_bars(self, code: str, trade_date: str) -> list[Bar]:
        volume_lots = 10000.0
        return [
            Bar(
                trade_date,
                minute,
                self.price,
                self.price,
                self.price,
                self.price,
                volume_lots,
                self.price * volume_lots * 100.0,
                volume_unit="LOT",
                limit_down=self.limit_down,
                time_semantics="INTERVAL_START",
                provider="TEST",
                price_adjustment="NONE",
            )
            for minute in ["11:00", "11:01", "11:02", "11:03", "11:04"]
        ]


class RawOutcomeMarket(FakeMarket):
    def __init__(self, raw_bar: Bar) -> None:
        super().__init__()
        self.raw_bar = raw_bar
        self.raw_calls = 0

    def raw_daily_bar(self, code: str, trade_date: str) -> Bar:
        self.raw_calls += 1
        return self.raw_bar


def add_t_day_facts(
    item: dict,
    *,
    stage_transition: str = "2→3",
    d_close: float = 10.0,
    limit_up: float = 11.0,
) -> None:
    item["candidates"][0]["source_values"] = {
        "premium_top10": {
            "晋阶": stage_transition,
            "sector": "测试行业",
            "close_T": d_close,
        },
        "decision_table": {
            "stage_transition": stage_transition,
            "industry": "测试行业",
            "d_close": d_close,
            "mechanism_limit_pct": 10.0,
            "estimated_up_limit": limit_up,
        },
    }


def mark_verified_t_close(trade: dict, value: float) -> None:
    trade["t_day_validation"].update(
        {
            "status": "VERIFIED",
            "trade_date": trade["buy_date"],
            "t_close": value,
            "provider": "TEST_RAW",
            "price_adjustment": "NONE",
            "reason": "test_verified_t_close",
        }
    )


class CapturingEastmoney(EastmoneyMarketData):
    def __init__(self) -> None:
        self.params: dict = {}

    def _request(self, params: dict) -> dict:
        self.params = params
        return {
            "data": {
                "klines": [
                    "2026-08-05 11:01,10,10,10.1,9.9,100,100000,0,0,0,0",
                    "2026-08-05 11:05,10,10,10.1,9.9,100,100000,0,0,0,0",
                ]
            }
        }


class AdjustedMinuteMarket(FakeMarket):
    def minute_bars(self, code: str, trade_date: str) -> list[Bar]:
        return [
            replace(item, price_adjustment="QFQ")
            for item in super().minute_bars(code, trade_date)
        ]


def auction_truth(
    *,
    filled_qty: int = 1000,
    submitted_qty: int = 1000,
    price: float | None = 10.0,
    reason: str | None = None,
    label_quality: str = "ACTUAL",
    auction_matched_qty: int | None = 200000,
    queue_ahead_qty: int | None = None,
    executable_qty_at_order: int | None = None,
) -> dict:
    if label_quality == "ACTUAL":
        source, data_tier = "BROKER_EXECUTION", "BROKER_LOG"
    elif label_quality == "REPLAY":
        source, data_tier = "EXCHANGE_ORDER_REPLAY", "ORDERBOOK"
    else:
        source, data_tier = "CONSERVATIVE_QUEUE_MODEL", "AUCTION_AGGREGATE"
    payload = {
        "schema_version": "auction_execution_v2",
        "event_at": "2026-08-04T09:25:00+08:00",
        "trade_date": "20260804",
        "ts_code": "600001.SH",
        "phase": "OPENING_CALL_AUCTION",
        "source": source,
        "data_tier": data_tier,
        "label_quality": label_quality,
        "quantity_unit": "SHARES",
        "submitted_qty": submitted_qty,
        "filled_qty": filled_qty,
        "limit_price": 10.2,
        "price": price,
        "auction_matched_qty": auction_matched_qty,
        "price_limit_source": "BROKER_ORDER_RECORD",
    }
    if reason is not None:
        payload["reason"] = reason
    if queue_ahead_qty is not None:
        payload["queue_ahead_qty"] = queue_ahead_qty
    if executable_qty_at_order is not None:
        payload["executable_qty_at_order"] = executable_qty_at_order
    return payload


def signal(candidate_count: int = 1) -> dict:
    candidates = [
        {
            "ts_code": f"60000{index}.SH",
            "name": f"票{index}",
            "rank": index,
            "metrics": {"utility_score": .01, "policy_trade_eligible": True},
            "features": {"rank_borda": 1.0 - index / 10.0},
            "source_ranks": {
                "a_top10": index,
                "premium_top10": index,
                "decision_table": index,
            },
            "action": "SHADOW",
            "action_reason": "test",
        }
        for index in range(1, candidate_count + 1)
    ]
    return {
        "signal_id": "s1",
        "decision_date": "20260803",
        "buy_date": "20260804",
        "exit_date": "20260805",
        "generated_at": "2026-08-03T20:00:00+08:00",
        "source_snapshots": [],
        "candidates": candidates,
        "model_version": "test",
        "status": "RANKED" if candidates else "NO_CANDIDATE",
    }


class LedgerDashboardTests(unittest.TestCase):
    def test_t_day_truth_waits_for_close_then_verifies_limit_up_and_promotion(self) -> None:
        state = empty_state()
        item = signal()
        add_t_day_facts(item)
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        market = RawOutcomeMarket(
            Bar(
                "20260804",
                None,
                10.1,
                11.0,
                11.0,
                10.0,
                1000,
                1000000,
                pct_change=10.0,
                volume_unit="LOT",
                provider="TEST_RAW",
                price_adjustment="NONE",
                previous_close=10.0,
            )
        )

        settle_trades(
            state,
            {"auctions": {}},
            market,
            EXECUTION,
            asof_at=datetime(2026, 8, 4, 11, 20, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        validation = state["trades"][0]["t_day_validation"]
        self.assertEqual(validation["status"], "PENDING")
        self.assertEqual(market.raw_calls, 0)

        settle_trades(
            state,
            {"auctions": {}},
            market,
            EXECUTION,
            asof_at=datetime(2026, 8, 4, 15, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        self.assertEqual(validation["status"], "VERIFIED")
        self.assertAlmostEqual(validation["t_return"], 0.10)
        self.assertTrue(validation["touched_limit_up"])
        self.assertTrue(validation["is_limit_up"])
        self.assertTrue(validation["is_promoted"])
        self.assertEqual(validation["stage_transition"], "2→3")

    def test_t_day_touch_without_close_limit_is_not_promoted(self) -> None:
        state = empty_state()
        item = signal()
        add_t_day_facts(item, stage_transition="3→4")
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        market = RawOutcomeMarket(
            Bar(
                "20260804",
                None,
                10.1,
                10.5,
                11.0,
                10.0,
                1000,
                1000000,
                volume_unit="LOT",
                provider="TEST_RAW",
                price_adjustment="NONE",
                previous_close=10.0,
            )
        )
        settle_trades(state, {"auctions": {}}, market, EXECUTION, asof_date="20260804")
        validation = state["trades"][0]["t_day_validation"]
        self.assertEqual(validation["status"], "VERIFIED")
        self.assertTrue(validation["touched_limit_up"])
        self.assertFalse(validation["is_limit_up"])
        self.assertFalse(validation["is_promoted"])

    def test_t_day_truth_is_independent_of_buy_fill_and_is_immutable(self) -> None:
        state = empty_state()
        item = signal()
        add_t_day_facts(item)
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        market = RawOutcomeMarket(
            Bar(
                "20260804",
                None,
                10.0,
                11.0,
                11.0,
                10.0,
                1000,
                1000000,
                volume_unit="LOT",
                provider="TEST_RAW",
                price_adjustment="NONE",
            )
        )
        no_fill = auction_truth(
            filled_qty=0,
            price=None,
            reason="NO_AUCTION_MATCH",
            auction_matched_qty=None,
        )
        settle_trades(
            state,
            {"auctions": {"20260804:600001.SH": no_fill}},
            market,
            EXECUTION,
            asof_date="20260804",
        )
        trade = state["trades"][0]
        self.assertEqual(trade["status"], "BUY_UNFILLED")
        self.assertTrue(trade["t_day_validation"]["is_promoted"])

        market.raw_bar = replace(market.raw_bar, close=10.0, high=10.0)
        settle_trades(state, {"auctions": {}}, market, EXECUTION, asof_date="20260805")
        self.assertTrue(trade["t_day_validation"]["is_promoted"])
        self.assertEqual(market.raw_calls, 1)

    def test_adjusted_or_conflicting_t_day_truth_is_not_fabricated(self) -> None:
        state = empty_state()
        item = signal()
        add_t_day_facts(item)
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        market = RawOutcomeMarket(
            Bar(
                "20260804",
                None,
                10.0,
                11.0,
                11.0,
                10.0,
                1000,
                1000000,
                volume_unit="LOT",
                provider="TEST_QFQ",
                price_adjustment="QFQ",
            )
        )
        settle_trades(state, {"auctions": {}}, market, EXECUTION, asof_date="20260804")
        validation = state["trades"][0]["t_day_validation"]
        self.assertEqual(validation["status"], "UNVERIFIABLE")
        self.assertIsNone(validation["t_return"])
        self.assertIsNone(validation["is_limit_up"])
        self.assertIsNone(validation["is_promoted"])

    def test_every_intersection_candidate_gets_one_idempotent_shadow_trade(self) -> None:
        state = empty_state()
        item = signal(candidate_count=5)
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        ensure_shadow_trades(state, item, [1, 2, 3])
        self.assertEqual(len(state["trades"]), 5)
        self.assertEqual([trade["rank"] for trade in state["trades"]], [1, 2, 3, 4, 5])
        self.assertTrue(
            all(trade["execution_mode"] == "SHADOW_ONLY" for trade in state["trades"])
        )

    def test_frozen_legacy_signal_is_migrated_and_missing_trades_are_backfilled(self) -> None:
        legacy = signal(candidate_count=3)
        state = empty_state()
        ensure_shadow_trades(state, legacy, [1, 2, 3])
        frozen = signal(candidate_count=5)
        for candidate in frozen["candidates"][3:]:
            candidate["action"] = "NO_TRADE"
            candidate["action_reason"] = "rank_outside_tracked_1_2_3"
            candidate["metrics"]["policy_trade_eligible"] = False
        state["signals"] = [frozen]

        _ensure_all_candidate_shadow_ledger(state, [1, 2, 3])
        _ensure_all_candidate_shadow_ledger(state, [1, 2, 3])

        self.assertEqual(len(state["trades"]), 5)
        self.assertTrue(
            all(candidate["action"] == "SHADOW" for candidate in frozen["candidates"])
        )
        self.assertEqual(
            frozen["candidates"][3]["action_audit"],
            [
                {
                    "action": "NO_TRADE",
                    "action_reason": "rank_outside_tracked_1_2_3",
                }
            ],
        )
        self.assertFalse(
            frozen["candidates"][3]["policy_decision"]["trade_eligible"]
        )
        self.assertFalse(
            frozen["candidates"][3]["policy_decision"]["broker_order_created"]
        )

    def test_candidate_slots_settle_independently_but_portfolio_waits_for_everyone(self) -> None:
        state = empty_state()
        item = signal(candidate_count=4)
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        second = auction_truth()
        second["ts_code"] = "600002.SH"
        fourth = auction_truth()
        fourth["ts_code"] = "600004.SH"
        truth = {
            "auctions": {
                "20260804:600002.SH": second,
                "20260804:600004.SH": fourth,
            }
        }
        settle_trades(
            state,
            truth,
            FakeMarket(),
            EXECUTION,
            asof_date="20260805",
        )
        by_rank = {trade["rank"]: trade for trade in state["trades"]}
        self.assertEqual(by_rank[1]["status"], "BUY_UNVERIFIABLE")
        self.assertEqual(by_rank[2]["status"], "CLOSED")
        self.assertEqual(by_rank[3]["status"], "BUY_UNVERIFIABLE")
        self.assertEqual(by_rank[4]["status"], "CLOSED")

        dashboard = build_dashboard(
            state,
            [],
            "2026-08-05T12:00:00+08:00",
            {"status": "RANKED"},
            [1, 2, 3],
        )
        validate_dashboard(dashboard)
        benchmark = dashboard["benchmark_daily"][0]
        self.assertEqual(benchmark["cohort_count"], 4)
        self.assertIsNone(
            benchmark["policies"]["all_candidates_equal_weight"][
                "portfolio_return"
            ]
        )
        self.assertIsNone(
            benchmark["policies"]["model_top3_equal_weight"][
                "portfolio_return"
            ]
        )
        self.assertTrue(
            benchmark["policies"]["fixed_model_rank_2"]["is_final"]
        )
        self.assertFalse(
            benchmark["policies"]["fixed_model_rank_1"]["is_final"]
        )
        self.assertEqual(
            dashboard["rank_daily"][0]["ranks"]["2"]["state"],
            "CLOSED",
        )
        self.assertIsNotNone(
            dashboard["rank_daily"][0]["ranks"]["2"]["daily_return"]
        )
        self.assertIsNone(dashboard["portfolio_daily"][0]["portfolio_return"])
        self.assertEqual(dashboard["portfolio_daily"][0]["final_count"], 2)
        self.assertEqual(dashboard["portfolio_daily"][0]["pending_count"], 2)

    def test_shadow_migration_is_idempotent_and_preserves_previous_reason(self) -> None:
        item = signal(candidate_count=1)
        candidate = item["candidates"][0]
        candidate["action"] = "NO_TRADE"
        candidate["action_reason"] = "legacy_policy_reason"
        self.assertTrue(migrate_signal_candidates_to_shadow(item))
        self.assertFalse(migrate_signal_candidates_to_shadow(item))
        self.assertEqual(len(candidate["action_audit"]), 1)
        self.assertIn("policy_gate=TRADE", candidate["action_reason"])

    def test_legacy_exact_truth_mode_never_invents_a_fill(self) -> None:
        state = empty_state()
        item = signal()
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        settle_trades(state, {"auctions": {}}, FakeMarket(), EXECUTION, asof_date="20260805")
        trade = state["trades"][0]
        self.assertEqual(trade["status"], "BUY_UNVERIFIABLE")
        self.assertIsNone(trade["buy"])

    def test_unadjusted_daily_open_becomes_full_shadow_fill(self) -> None:
        state = empty_state()
        item = signal()
        add_t_day_facts(item, limit_up=11.0)
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        market = RawOutcomeMarket(
            Bar(
                "20260804",
                None,
                10.0,
                10.2,
                10.3,
                9.8,
                1000,
                1000000,
                provider="TEST_RAW",
                price_adjustment="NONE",
            )
        )

        settle_trades(
            state,
            {"auctions": {}},
            market,
            OPEN_FILL_EXECUTION,
            asof_date="20260804",
        )

        trade = state["trades"][0]
        self.assertEqual(trade["status"], "OPEN")
        self.assertEqual(trade["buy"]["avg_price"], 10.0)
        self.assertEqual(trade["buy"]["submitted_qty"], 9000)
        self.assertEqual(trade["buy"]["filled_qty"], 9000)
        self.assertEqual(
            trade["buy"]["price_policy"],
            "T_DAILY_UNADJUSTED_OPEN",
        )
        self.assertEqual(
            trade["diagnostics"]["daily_open_fill"]["quantity_policy"],
            "FROZEN_LIMIT_PRICE_MIGRATION",
        )
        self.assertTrue(
            trade["diagnostics"]["daily_open_fill"]["counts_as_fill"]
        )

    def test_legacy_unverifiable_trade_retries_open_and_closes(self) -> None:
        state = empty_state()
        item = signal()
        add_t_day_facts(item, limit_up=11.0)
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        market = RawOutcomeMarket(
            Bar(
                "20260804",
                None,
                10.0,
                10.2,
                10.3,
                9.8,
                1000,
                1000000,
                provider="TEST_RAW",
                price_adjustment="NONE",
            )
        )
        settle_trades(
            state,
            {"auctions": {}},
            market,
            EXECUTION,
            asof_date="20260804",
        )
        self.assertEqual(state["trades"][0]["status"], "BUY_UNVERIFIABLE")

        settle_trades(
            state,
            {"auctions": {}},
            market,
            OPEN_FILL_EXECUTION,
            asof_date="20260805",
        )
        trade = state["trades"][0]
        self.assertEqual(trade["status"], "CLOSED")
        self.assertEqual(trade["buy"]["avg_price"], 10.0)
        self.assertIsInstance(trade["pnl"]["net_return_on_allocated"], float)

    def test_adjusted_or_wrong_date_open_cannot_become_fill(self) -> None:
        for bar in (
            Bar(
                "20260804",
                None,
                10.0,
                10.2,
                10.3,
                9.8,
                1000,
                1000000,
                provider="TEST_QFQ",
                price_adjustment="QFQ",
            ),
            Bar(
                "20260803",
                None,
                10.0,
                10.2,
                10.3,
                9.8,
                1000,
                1000000,
                provider="TEST_RAW",
                price_adjustment="NONE",
            ),
        ):
            with self.subTest(bar=bar):
                state = empty_state()
                item = signal()
                add_t_day_facts(item, limit_up=11.0)
                state["signals"].append(item)
                ensure_shadow_trades(state, item, [1, 2, 3])
                settle_trades(
                    state,
                    {"auctions": {}},
                    RawOutcomeMarket(bar),
                    OPEN_FILL_EXECUTION,
                    asof_date="20260804",
                )
                trade = state["trades"][0]
                self.assertEqual(trade["status"], "BUY_UNVERIFIABLE")
                self.assertIsNone(trade["buy"])

    def test_open_above_frozen_limit_is_unfilled(self) -> None:
        state = empty_state()
        item = signal()
        add_t_day_facts(item, limit_up=11.0)
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        market = RawOutcomeMarket(
            Bar(
                "20260804",
                None,
                11.1,
                11.2,
                11.3,
                11.0,
                1000,
                1000000,
                provider="TEST_RAW",
                price_adjustment="NONE",
            )
        )
        settle_trades(
            state,
            {"auctions": {}},
            market,
            OPEN_FILL_EXECUTION,
            asof_date="20260804",
        )
        trade = state["trades"][0]
        self.assertEqual(trade["status"], "BUY_UNFILLED")
        self.assertEqual(trade["buy"]["filled_qty"], 0)
        self.assertIsNone(trade["buy"]["avg_price"])

    def test_exact_auction_truth_and_five_minutes_close_trade(self) -> None:
        state = empty_state()
        item = signal()
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        truth = {
            "auctions": {
                "20260804:600001.SH": auction_truth()
            }
        }
        settle_trades(state, truth, FakeMarket(), EXECUTION, asof_date="20260805")
        trade = state["trades"][0]
        self.assertEqual(trade["status"], "CLOSED")
        self.assertEqual(trade["exit"]["remaining_qty"], 0)
        self.assertEqual(trade["exit"]["actual_exit_at"], "20260805T11:04:00+08:00")
        self.assertIsInstance(trade["pnl"]["net_return_on_allocated"], float)

    def test_incomplete_exit_is_not_fabricated(self) -> None:
        state = empty_state()
        item = signal()
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        truth = {"auctions": {"20260804:600001.SH": auction_truth()}}
        settle_trades(state, truth, FakeMarket(complete=False), EXECUTION, asof_date="20260805")
        trade = state["trades"][0]
        self.assertEqual(trade["status"], "EXIT_UNVERIFIABLE")
        self.assertIsNone(trade["pnl"])

    def test_adjusted_minute_prices_are_rejected_for_execution(self) -> None:
        state = empty_state()
        item = signal()
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        truth = {"auctions": {"20260804:600001.SH": auction_truth()}}
        settle_trades(
            state,
            truth,
            AdjustedMinuteMarket(),
            EXECUTION,
            asof_date="20260805",
        )
        trade = state["trades"][0]
        self.assertEqual(trade["status"], "EXIT_UNVERIFIABLE")
        self.assertEqual(
            trade["exit"]["attempts"][0]["invalid_price_adjustments"],
            ["11:00", "11:01", "11:02", "11:03", "11:04"],
        )

    def test_reliable_zero_fill_needs_no_price_and_closes_as_cash(self) -> None:
        state = empty_state()
        item = signal()
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        truth = {
            "auctions": {
                "20260804:600001.SH": auction_truth(
                    filled_qty=0,
                    price=None,
                    reason="SUSPENDED",
                    auction_matched_qty=None,
                )
            }
        }
        settle_trades(state, truth, FakeMarket(), EXECUTION, asof_date="20260805")
        trade = state["trades"][0]
        self.assertEqual(trade["status"], "BUY_UNFILLED")
        self.assertEqual(trade["buy"]["filled_qty"], 0)
        self.assertIsNone(trade["buy"]["avg_price"])

    def test_legacy_or_wrong_time_auction_truth_fails_closed(self) -> None:
        state = empty_state()
        item = signal()
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        invalid = auction_truth()
        invalid["event_at"] = "2026-08-04T09:24:59+08:00"
        settle_trades(
            state,
            {"auctions": {"20260804:600001.SH": invalid}},
            FakeMarket(),
            EXECUTION,
            asof_date="20260805",
        )
        trade = state["trades"][0]
        self.assertEqual(trade["status"], "BUY_UNVERIFIABLE")
        self.assertIn("exactly 09:25:00", trade["diagnostics"]["auction_truth_error"])
        self.assertIsNone(trade["buy"])

    def test_non_actual_positive_fill_requires_queue_evidence(self) -> None:
        state = empty_state()
        item = signal()
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        invalid = auction_truth(label_quality="REPLAY")
        settle_trades(
            state,
            {"auctions": {"20260804:600001.SH": invalid}},
            FakeMarket(),
            EXECUTION,
            asof_date="20260805",
        )
        trade = state["trades"][0]
        self.assertEqual(trade["status"], "BUY_UNVERIFIABLE")
        self.assertIn("queue_ahead_qty", trade["diagnostics"]["auction_truth_error"])

    def test_conservative_aggregate_cannot_claim_a_positive_fill(self) -> None:
        state = empty_state()
        item = signal()
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        conservative = auction_truth(label_quality="CONSERVATIVE")
        settle_trades(
            state,
            {"auctions": {"20260804:600001.SH": conservative}},
            FakeMarket(),
            EXECUTION,
            asof_date="20260805",
        )
        trade = state["trades"][0]
        self.assertEqual(trade["status"], "BUY_UNVERIFIABLE")
        self.assertIn("full order-book queue evidence", trade["diagnostics"]["auction_truth_error"])

    def test_replay_positive_fill_with_queue_evidence_is_accepted(self) -> None:
        state = empty_state()
        item = signal()
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        replay = auction_truth(
            label_quality="REPLAY",
            queue_ahead_qty=500,
            executable_qty_at_order=2000,
        )
        settle_trades(
            state,
            {"auctions": {"20260804:600001.SH": replay}},
            FakeMarket(),
            EXECUTION,
            asof_date="20260805",
        )
        self.assertEqual(state["trades"][0]["status"], "CLOSED")

    def test_actual_broker_fill_is_kept_when_participation_cap_is_breached(self) -> None:
        state = empty_state()
        item = signal()
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        actual = auction_truth(auction_matched_qty=10000)
        settle_trades(
            state,
            {"auctions": {"20260804:600001.SH": actual}},
            FakeMarket(),
            EXECUTION,
            asof_date="20260805",
        )
        diagnostic = state["trades"][0]["diagnostics"]["auction_participation_cap_breached"]
        self.assertTrue(diagnostic["accepted_as_actual_broker_fact"])

    def test_locked_limit_down_delays_then_next_trading_day_closes_without_duplicates(self) -> None:
        state = empty_state()
        item = signal()
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        truth = {"auctions": {"20260804:600001.SH": auction_truth()}}
        market = FakeMarket(locked_dates={"20260805"})
        settle_trades(state, truth, market, EXECUTION, asof_date="20260805")
        trade = state["trades"][0]
        self.assertEqual(trade["status"], "EXIT_DELAYED")
        self.assertEqual(trade["exit"]["filled_qty"], 0)
        self.assertEqual(trade["exit"]["processed_windows"], ["20260805"])

        settle_trades(state, truth, market, EXECUTION, asof_date="20260806")
        self.assertEqual(trade["status"], "CLOSED")
        self.assertEqual(trade["exit"]["actual_exit_date"], "20260806")
        self.assertEqual(trade["exit"]["delay_trading_days"], 1)
        self.assertTrue(all(fill["date"] == "20260806" for fill in trade["exit"]["fills"]))
        fill_count = len(trade["exit"]["fills"])
        settle_trades(state, truth, market, EXECUTION, asof_date="20260806")
        self.assertEqual(len(trade["exit"]["fills"]), fill_count)

    def test_one_price_limit_up_without_explicit_limit_down_is_sellable(self) -> None:
        state = empty_state()
        item = signal()
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        trade = state["trades"][0]
        mark_verified_t_close(trade, 10.0)

        settle_trades(
            state,
            {"auctions": {"20260804:600001.SH": auction_truth()}},
            OnePriceNoLimitMarket(11.0),
            EXECUTION,
            asof_date="20260805",
        )

        self.assertEqual(trade["status"], "CLOSED")
        self.assertEqual(trade["exit"]["remaining_qty"], 0)
        self.assertTrue(
            all(
                check["reason"] == "one_price_up_from_previous_close"
                and check["decision"] == "SELLABLE"
                and check["previous_close"] == 10.0
                and check["previous_close_source"] == "VERIFIED_T_DAY_CLOSE"
                for check in trade["exit"]["attempts"][0]["lock_checks"]
            )
        )

    def test_one_price_down_without_explicit_limit_down_is_conservatively_delayed(self) -> None:
        state = empty_state()
        item = signal()
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        trade = state["trades"][0]
        mark_verified_t_close(trade, 10.0)

        settle_trades(
            state,
            {"auctions": {"20260804:600001.SH": auction_truth()}},
            OnePriceNoLimitMarket(9.0),
            EXECUTION,
            asof_date="20260805",
        )

        self.assertEqual(trade["status"], "EXIT_DELAYED")
        self.assertEqual(trade["exit"]["remaining_qty"], 1000)
        self.assertEqual(trade["exit"]["processed_windows"], ["20260805"])
        self.assertTrue(
            all(
                check["reason"] == "one_price_down_from_previous_close_conservative"
                and check["decision"] == "LOCKED"
                for check in trade["exit"]["attempts"][0]["lock_checks"]
            )
        )

    def test_one_price_flat_without_direction_truth_fails_closed(self) -> None:
        state = empty_state()
        item = signal()
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        trade = state["trades"][0]
        mark_verified_t_close(trade, 10.0)

        settle_trades(
            state,
            {"auctions": {"20260804:600001.SH": auction_truth()}},
            OnePriceNoLimitMarket(10.0),
            EXECUTION,
            asof_date="20260805",
        )

        self.assertEqual(trade["status"], "EXIT_UNVERIFIABLE")
        self.assertEqual(trade["reason"], "one_price_exit_direction_unverifiable")
        self.assertEqual(trade["exit"]["processed_windows"], [])
        attempt = trade["exit"]["attempts"][0]
        self.assertEqual(attempt["result"], "UNVERIFIABLE")
        self.assertEqual(
            attempt["lock_checks"][0]["reason"],
            "one_price_direction_flat_or_unknown",
        )

    def test_legacy_all_locked_window_is_reopened_once_and_audited(self) -> None:
        state = empty_state()
        item = signal()
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        trade = state["trades"][0]
        mark_verified_t_close(trade, 10.0)
        truth = {"auctions": {"20260804:600001.SH": auction_truth()}}

        # First create the durable shape emitted by the old all-one-price lock
        # rule, then remove the new directional evidence to model legacy state.
        settle_trades(
            state,
            truth,
            OnePriceNoLimitMarket(11.0, limit_down=11.0),
            EXECUTION,
            asof_date="20260805",
        )
        self.assertEqual(trade["status"], "EXIT_DELAYED")
        legacy_attempt = trade["exit"]["attempts"][0]
        legacy_attempt.pop("lock_inference_version")
        legacy_attempt.pop("lock_checks")

        settle_trades(
            state,
            truth,
            OnePriceNoLimitMarket(11.0),
            EXECUTION,
            asof_date="20260805",
        )

        self.assertEqual(trade["status"], "CLOSED")
        self.assertEqual(trade["exit"]["processed_windows"], ["20260805"])
        self.assertEqual(trade["exit"]["attempts"][0]["attempt_count"], 2)
        self.assertEqual(len(trade["exit"]["legacy_lock_rechecks"]), 1)
        audit = trade["exit"]["legacy_lock_rechecks"][0]
        self.assertEqual(
            audit["reason"],
            "legacy_one_price_lock_lacked_direction_evidence",
        )
        self.assertEqual(audit["previous_attempt"]["result"], "DELAYED")
        fill_ids = [fill["fill_id"] for fill in trade["exit"]["fills"]]
        self.assertEqual(len(fill_ids), len(set(fill_ids)))

    def test_partial_exit_persists_fills_fees_and_retries_next_trading_day(self) -> None:
        state = empty_state()
        item = signal()
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        truth = {"auctions": {"20260804:600001.SH": auction_truth()}}
        market = FakeMarket(volume_lots_by_date={"20260805": 20, "20260806": 10000})
        settle_trades(state, truth, market, EXECUTION, asof_date="20260805")
        trade = state["trades"][0]
        self.assertEqual(trade["status"], "EXIT_DELAYED")
        self.assertEqual(trade["exit"]["filled_qty"], 500)
        self.assertEqual(trade["exit"]["remaining_qty"], 500)
        first_fees = trade["exit"]["fees"]
        self.assertEqual(first_fees, sum(fill["fees"] for fill in trade["exit"]["fills"]))

        settle_trades(state, truth, market, EXECUTION, asof_date="20260806")
        self.assertEqual(trade["status"], "CLOSED")
        self.assertEqual(trade["exit"]["filled_qty"], 1000)
        self.assertGreater(trade["exit"]["fees"], first_fees)
        self.assertEqual(trade["exit"]["fees"], sum(fill["fees"] for fill in trade["exit"]["fills"]))

    def test_unverifiable_window_is_retried_without_duplicate_fills(self) -> None:
        state = empty_state()
        item = signal()
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        truth = {"auctions": {"20260804:600001.SH": auction_truth()}}
        market = FakeMarket(incomplete_dates={"20260805"})
        settle_trades(state, truth, market, EXECUTION, asof_date="20260805")
        trade = state["trades"][0]
        self.assertEqual(trade["status"], "EXIT_UNVERIFIABLE")
        self.assertEqual(trade["exit"]["processed_windows"], [])
        market.incomplete_dates.clear()
        settle_trades(state, truth, market, EXECUTION, asof_date="20260805")
        self.assertEqual(trade["status"], "CLOSED")
        self.assertEqual(len({fill["fill_id"] for fill in trade["exit"]["fills"]}), len(trade["exit"]["fills"]))
        self.assertEqual(trade["exit"]["attempts"][0]["attempt_count"], 2)

    def test_exit_window_and_five_percent_participation_are_read_from_config(self) -> None:
        with Path("config/system.json").open("r", encoding="utf-8") as handle:
            config = json.load(handle)["execution"]
        self.assertEqual(config["max_exit_participation_rate"], .05)
        custom = dict(EXECUTION)
        custom.update({"exit_window_start": "11:01", "exit_window_end": "11:03", "exit_minute_count": 2})
        state = empty_state()
        item = signal()
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        settle_trades(
            state,
            {"auctions": {"20260804:600001.SH": auction_truth()}},
            FakeMarket(),
            custom,
            asof_date="20260805",
        )
        fills = state["trades"][0]["exit"]["fills"]
        self.assertEqual([fill["minute"] for fill in fills], ["11:01", "11:02"])

    def test_minimum_commission_is_calculated_per_child_order(self) -> None:
        state = empty_state()
        item = signal()
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        settle_trades(
            state,
            {"auctions": {"20260804:600001.SH": auction_truth(filled_qty=600)}},
            FakeMarket(),
            EXECUTION,
            asof_date="20260805",
        )
        exit_payload = state["trades"][0]["exit"]
        expected = sum(
            max(5.0, fill["amount"] * .0003) + fill["amount"] * (.00001 + .0005)
            for fill in exit_payload["fills"]
        )
        self.assertAlmostEqual(exit_payload["fees"], expected)

    def test_period_end_labels_are_normalized_to_exact_exit_interval(self) -> None:
        self.assertEqual(period_end_to_interval_start("2026-08-05", "11:01"), ("20260805", "11:00"))
        self.assertEqual(period_end_to_interval_start("2026-08-05", "11:05"), ("20260805", "11:04"))
        market = CapturingEastmoney()
        bars = market.minute_bars("600001.SH", "20260805")
        self.assertEqual(market.params["fqt"], 0)
        self.assertEqual([bar.source_time for bar in bars], ["11:01", "11:05"])
        self.assertEqual([bar.time for bar in bars], ["11:00", "11:04"])

    def test_fixed_ranks_are_not_backfilled(self) -> None:
        state = empty_state()
        item = signal(candidate_count=2)
        state["signals"].append(item)
        ensure_shadow_trades(state, item, [1, 2, 3])
        dashboard = build_dashboard(state, [], "2026-08-04T20:00:00+08:00", {"status": "RANKED"}, [1, 2, 3])
        validate_dashboard(dashboard)
        self.assertEqual(dashboard["days"][0]["rank_slots"]["3"]["status"], "NOT_AVAILABLE")
        self.assertEqual(dashboard["rank_daily"][0]["ranks"]["3"]["daily_return"], 0.0)

    def test_zero_intersection_creates_three_empty_fixed_slots(self) -> None:
        state = empty_state()
        item = signal(candidate_count=0)
        state["signals"].append(item)
        dashboard = build_dashboard(state, [], "2026-08-04T20:00:00+08:00", {"status": "NO_CANDIDATE"}, [1, 2, 3])
        self.assertEqual(dashboard["days"][0]["intersection_count"], 0)
        self.assertEqual({slot["status"] for slot in dashboard["days"][0]["rank_slots"].values()}, {"NOT_AVAILABLE"})


if __name__ == "__main__":
    unittest.main()
