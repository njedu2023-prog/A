from __future__ import annotations

import copy
import unittest
from dataclasses import replace

from three_table_quant.domain import Candidate
from three_table_quant.dashboard import build_dashboard, validate_dashboard
from three_table_quant.execution_policy import build_order_spec
from three_table_quant.limit_lifecycle import EXPECTED_SESSION_MINUTES
from three_table_quant.market import Bar
from three_table_quant.single_stock_collection import (
    LIFECYCLE_INPUT_FIELD,
    build_candidate_single_stock_research,
)
from three_table_quant.single_stock_research import (
    AUDIT_ONLY_EFFECT,
    RESEARCH_SNAPSHOT_HASH_FIELD,
    SINGLE_STOCK_RESEARCH_SCHEMA,
    research_snapshot_sha256,
    unavailable_single_stock_research,
)
from three_table_quant.single_stock_v3 import HardGateStatus
from three_table_quant.sources import SOURCE_A, SOURCE_DECISION, SOURCE_PREMIUM


DAY = "20260807"
ASOF = "2026-08-07T21:30:00+08:00"


def execution() -> dict[str, object]:
    return {
        "slot_capital_cny": 100000.0,
        "lot_size": 100,
        "price_tick": 0.01,
        "auction_time": "09:25",
        "auction_phase": "OPENING_CALL_AUCTION",
        "commission_rate": 0.0003,
        "minimum_commission_cny": 5.0,
        "transfer_fee_rate_each_side": 0.00001,
    }


def candidate() -> Candidate:
    item = Candidate(
        ts_code="000815.SZ",
        name="美利云",
        source_ranks={SOURCE_A: 2, SOURCE_PREMIUM: 7, SOURCE_DECISION: 4},
        source_values={
            SOURCE_A: {"trade_date": DAY, "advance_stage": "2→3", "board": "IT服务"},
            SOURCE_PREMIUM: {
                "trade_date": DAY,
                "close_T": 10.0,
                "stage_transition": "2→3",
                "sector": "IT服务",
            },
            SOURCE_DECISION: {
                "stage_transition": "2→3",
                "industry": "IT服务",
                "d_close": 10.0,
                "mechanism_limit_pct": 10.0,
                "estimated_up_limit": 11.0,
            },
        },
        features={
            "feature_schema_version": "formal_features_v2",
            "feature_asof_date": DAY,
            "feature_coverage": 1.0,
            "market_data_valid": True,
            "market_data_invalid_reasons": [],
            "stage_transition": "2→3",
            "rank_borda": 0.9,
            "ret_1d": 0.0,
        },
        metrics={
            "model_id": "transparent_shadow_baseline_v2",
            "model_stage": "CHAMPION_BASELINE",
            "expected_net_return": 0.01,
            "conditional_net_return_mean": 0.01,
            "gate_decision": "TRADE",
            "gate_reasons": [],
            "policy_trade_eligible": True,
        },
        rank=1,
        action="SHADOW",
        action_reason="frozen",
    )
    item.order_spec = build_order_spec(
        item,
        decision_date=DAY,
        buy_date="20260810",
        execution=execution(),
    )
    return item


def minutes(count: int = 240) -> list[Bar]:
    return [
        Bar(
            date=DAY,
            time=time,
            open=9.9,
            close=9.9,
            high=9.9,
            low=9.9,
            volume=1000.0,
            amount=9900.0,
            volume_unit="SHARE",
            time_semantics="INTERVAL_START",
            provider="TEST",
            price_adjustment="NONE",
            price_tick=0.01,
        )
        for time in EXPECTED_SESSION_MINUTES[:count]
    ]


class SingleStockCollectionTests(unittest.TestCase):
    def test_freezes_full_d_day_research_without_mutating_decision_inputs(self) -> None:
        item = candidate()
        before = copy.deepcopy(item.to_dict())

        payload = build_candidate_single_stock_research(
            item,
            decision_date=DAY,
            decision_asof=ASOF,
            execution=execution(),
            minute_bars=minutes(),
        )

        self.assertEqual(payload["decision_effect"], AUDIT_ONLY_EFFECT)
        self.assertEqual(payload["availability"], "AVAILABLE")
        self.assertEqual(
            payload[RESEARCH_SNAPSHOT_HASH_FIELD],
            research_snapshot_sha256(payload),
        )
        self.assertEqual(
            payload["single_stock"]["hard_gate"]["status"],
            HardGateStatus.UNKNOWN.value,
        )
        self.assertEqual(payload["limit_lifecycle"]["availability"], "AVAILABLE")
        self.assertEqual(
            payload["limit_lifecycle"]["payload"]["observed_minutes"],
            240,
        )
        self.assertEqual(item.to_dict(), before)

    def test_incomplete_minutes_are_unknown_not_zero_lifecycle(self) -> None:
        payload = build_candidate_single_stock_research(
            candidate(),
            decision_date=DAY,
            decision_asof=ASOF,
            execution=execution(),
            minute_bars=minutes(5),
        )

        lifecycle = payload["limit_lifecycle"]
        self.assertEqual(lifecycle["availability"], "UNAVAILABLE")
        self.assertIsNone(lifecycle["payload"])
        self.assertIn("minute_coverage_incomplete", lifecycle["unavailable_reason"])

    def test_fetch_failure_is_explicit_missing_evidence(self) -> None:
        payload = build_candidate_single_stock_research(
            candidate(),
            decision_date=DAY,
            decision_asof=ASOF,
            execution=execution(),
            minute_bars=None,
            minute_fetch_failed=True,
        )

        fact = payload["single_stock"]["facts"][LIFECYCLE_INPUT_FIELD]
        self.assertIsNone(fact["value"])
        self.assertEqual(fact["missing_reason"], "MINUTE_FETCH_FAILED")
        self.assertEqual(payload["limit_lifecycle"]["availability"], "UNAVAILABLE")

    def test_minute_content_digest_binds_actual_ohlcv_values(self) -> None:
        base_bars = minutes()
        changed_bars = list(base_bars)
        changed_bars[0] = replace(
            changed_bars[0],
            close=9.8,
            low=9.8,
            amount=9800.0,
        )
        base = build_candidate_single_stock_research(
            candidate(),
            decision_date=DAY,
            decision_asof=ASOF,
            execution=execution(),
            minute_bars=base_bars,
        )
        changed = build_candidate_single_stock_research(
            candidate(),
            decision_date=DAY,
            decision_asof=ASOF,
            execution=execution(),
            minute_bars=changed_bars,
        )

        base_fact = base["single_stock"]["facts"][LIFECYCLE_INPUT_FIELD]
        changed_fact = changed["single_stock"]["facts"][LIFECYCLE_INPUT_FIELD]
        self.assertNotEqual(
            base_fact["value"]["bars_content_sha256"],
            changed_fact["value"]["bars_content_sha256"],
        )
        self.assertNotEqual(
            base_fact["provenance"]["content_sha256"],
            changed_fact["provenance"]["content_sha256"],
        )

    def test_missing_pricing_is_unknown_instead_of_a_false_block(self) -> None:
        item = candidate()
        item.source_values[SOURCE_DECISION]["d_close"] = None
        item.source_values[SOURCE_PREMIUM]["close_T"] = None
        payload = build_candidate_single_stock_research(
            item,
            decision_date=DAY,
            decision_asof=ASOF,
            execution=execution(),
            minute_bars=minutes(),
        )

        pricing = payload["single_stock"]["facts"]["market.pricing_verified"]
        self.assertIsNone(pricing["value"])
        self.assertEqual(pricing["missing_reason"], "FROZEN_PRICING_EVIDENCE_INCOMPLETE")
        self.assertEqual(payload["single_stock"]["hard_gate"]["status"], "UNKNOWN")

    def test_dashboard_copies_same_research_and_detects_tampering(self) -> None:
        item = candidate()
        item.single_stock_research = build_candidate_single_stock_research(
            item,
            decision_date=DAY,
            decision_asof=ASOF,
            execution=execution(),
            minute_bars=minutes(),
        )
        signal = {
            "signal_id": "20260807-v3",
            "decision_date": DAY,
            "buy_date": "20260810",
            "exit_date": "20260811",
            "generated_at": ASOF,
            "source_snapshots": [],
            "candidates": [item.to_dict()],
            "model_version": "transparent_shadow_baseline_v2",
            "status": "RANKED",
            "single_stock_research_schema_version": SINGLE_STOCK_RESEARCH_SCHEMA,
        }
        trade = {
            "trade_id": f"{DAY}:R1",
            "signal_id": signal["signal_id"],
            "decision_date": DAY,
            "buy_date": "20260810",
            "planned_exit_date": "20260811",
            "rank": 1,
            "ts_code": item.ts_code,
            "name": item.name,
            "status": "PENDING_BUY",
            "reason": "test",
            "buy": None,
            "exit": None,
            "pnl": None,
            "diagnostics": {},
        }
        dashboard = build_dashboard(
            {"signals": [signal], "trades": [trade]},
            [],
            ASOF,
            {"status": "RANKED"},
            [1, 2, 3],
        )
        validate_dashboard(dashboard)
        ranked = dashboard["days"][0]["candidates"][0]["single_stock_research"]
        ledger = dashboard["portfolio_daily"][0]["candidates"][0][
            "single_stock_research"
        ]
        self.assertEqual(ranked, ledger)

        lifecycle_tamper = copy.deepcopy(dashboard)
        for location in (
            lifecycle_tamper["days"][0]["candidates"][0]["single_stock_research"],
            lifecycle_tamper["portfolio_daily"][0]["candidates"][0][
                "single_stock_research"
            ],
        ):
            location["limit_lifecycle"]["payload"]["sealed_minutes"] = 999
            location[RESEARCH_SNAPSHOT_HASH_FIELD] = research_snapshot_sha256(location)
        with self.assertRaisesRegex(ValueError, "sealed_minutes"):
            validate_dashboard(lifecycle_tamper)

        for copy_payload in (ranked, ledger):
            copy_payload["single_stock"]["hard_gate"]["status"] = "PASS"
            copy_payload[RESEARCH_SNAPSHOT_HASH_FIELD] = research_snapshot_sha256(
                copy_payload
            )
        with self.assertRaisesRegex(ValueError, "not canonical"):
            validate_dashboard(dashboard)

    def test_v3_marker_requires_research_but_typed_unavailable_is_valid(self) -> None:
        item = candidate()
        item.single_stock_research = unavailable_single_stock_research(
            item,
            decision_date=DAY,
            decision_asof=ASOF,
            reason="MINUTE_RESEARCH_BUDGET_EXHAUSTED",
        )
        signal = {
            "signal_id": "20260807-v3-unavailable",
            "decision_date": DAY,
            "buy_date": "20260810",
            "exit_date": "20260811",
            "generated_at": ASOF,
            "source_snapshots": [],
            "candidates": [item.to_dict()],
            "model_version": "transparent_shadow_baseline_v2",
            "status": "RANKED",
            "single_stock_research_schema_version": SINGLE_STOCK_RESEARCH_SCHEMA,
        }
        trade = {
            "trade_id": f"{DAY}:R1",
            "signal_id": signal["signal_id"],
            "decision_date": DAY,
            "buy_date": "20260810",
            "planned_exit_date": "20260811",
            "rank": 1,
            "ts_code": item.ts_code,
            "name": item.name,
            "status": "PENDING_BUY",
            "reason": "test",
            "buy": None,
            "exit": None,
            "pnl": None,
            "diagnostics": {},
        }
        dashboard = build_dashboard(
            {"signals": [signal], "trades": [trade]},
            [],
            ASOF,
            {"status": "RANKED"},
            [1, 2, 3],
        )
        validate_dashboard(dashboard)

        dashboard["days"][0]["candidates"][0]["single_stock_research"] = None
        with self.assertRaisesRegex(ValueError, "requires single-stock research"):
            validate_dashboard(dashboard)


if __name__ == "__main__":
    unittest.main()
