from __future__ import annotations

import unittest

from three_table_quant.domain import Candidate, ContractError
from three_table_quant.execution_policy import ORDER_SPEC_SCHEMA
from three_table_quant.limit_lifecycle import LimitLifecycleSnapshot, MINUTE_CLOSE_PROXY
from three_table_quant.single_stock_research import (
    AUDIT_ONLY_EFFECT,
    AuditAvailability,
    build_single_stock_research_snapshot,
)
from three_table_quant.single_stock_v3 import (
    BOARD_LOT_FIELD,
    D_CLOSE_FIELD,
    DELISTING_FIELD,
    MAX_ORDER_SHARES_FIELD,
    PRICE_TICK_FIELD,
    PRICING_VERIFIED_FIELD,
    SUSPENDED_FIELD,
    TRADING_RULES_VERIFIED_FIELD,
    FactProvenance,
    HardGateStatus,
    SingleStockFact,
    SingleStockSnapshotV3,
)
from three_table_quant.sources import SOURCE_A, SOURCE_DECISION, SOURCE_PREMIUM


DAY = "20260807"
RANKS = {SOURCE_A: 2, SOURCE_PREMIUM: 7, SOURCE_DECISION: 4}


def provenance() -> FactProvenance:
    return FactProvenance(
        provider="TEST",
        dataset_version="v1",
        known_at="2026-08-07T15:01:00+08:00",
        fetched_at="2026-08-07T21:00:00+08:00",
        content_sha256="b" * 64,
    )


def single_stock() -> SingleStockSnapshotV3:
    facts = {
        SUSPENDED_FIELD: SingleStockFact(False, provenance()),
        DELISTING_FIELD: SingleStockFact(False, provenance()),
        TRADING_RULES_VERIFIED_FIELD: SingleStockFact(True, provenance()),
        PRICING_VERIFIED_FIELD: SingleStockFact(True, provenance()),
        D_CLOSE_FIELD: SingleStockFact(10.0, provenance()),
        PRICE_TICK_FIELD: SingleStockFact(0.01, provenance()),
        BOARD_LOT_FIELD: SingleStockFact(100, provenance()),
        MAX_ORDER_SHARES_FIELD: SingleStockFact(9000, provenance()),
    }
    return SingleStockSnapshotV3(
        ts_code="000815.SZ",
        name="美利云",
        decision_date=DAY,
        decision_asof="2026-08-07T21:30:00+08:00",
        source_ranks=RANKS,
        facts=facts,
    )


def candidate() -> Candidate:
    item = Candidate(
        ts_code="000815.SZ",
        name="美利云",
        source_ranks=dict(RANKS),
        source_values={source: {} for source in RANKS},
        features={
            "feature_schema_version": "formal_features_v2",
            "feature_asof_date": DAY,
            "feature_coverage": 1.0,
            "market_data_valid": True,
            "market_data_invalid_reasons": [],
            "stage_transition": "3→4",
            "ret_1d": 0.0,
            "ret_5d": 0.05,
            "ret_20d": 0.1,
            "volatility_20d": 0.02,
        },
        metrics={
            "model_id": "transparent_shadow_baseline_v2",
            "model_stage": "TRANSPARENT_BASELINE",
            "expected_net_return": 0.0,
            "p_promotion": 0.42,
            "gate_decision": "TRADE",
            "gate_reasons": [],
        },
        rank=1,
        action="SHADOW",
        action_reason="audit fixture",
    )
    item.order_spec = {
        "schema_version": ORDER_SPEC_SCHEMA,
        "decision_date": DAY,
        "trade_date": "20260810",
        "event_time": "09:25",
        "phase": "OPENING_CALL_AUCTION",
        "side": "BUY",
        "order_type": "LIMIT",
        "limit_price_policy": "FROZEN_D_LIMIT_UP_MARKETABLE_LIMIT",
        "limit_price": 11.0,
        "price_limit_source": "D_CLOSE_MECHANISM_ROUND_HALF_UP",
        "submitted_qty": 9000,
        "quantity_unit": "SHARES",
        "lot_size": 100,
        "slot_capital_cny": 100000.0,
        "maximum_reserved_cash_cny": 99034.7,
        "execution_mode": "SHADOW_ONLY",
    }
    return item


def lifecycle(*, valid: bool = True, day: str = DAY) -> LimitLifecycleSnapshot:
    return LimitLifecycleSnapshot(
        schema_version="limit_lifecycle_v1",
        decision_date=day,
        evidence_level=MINUTE_CLOSE_PROXY,
        valid=valid,
        invalid_reasons=() if valid else ("minute_coverage_incomplete",),
        limit_price=11.0,
        price_tick=0.01,
        expected_minutes=240,
        observed_minutes=240 if valid else 239,
        coverage_ratio=1.0 if valid else 239 / 240,
        touched_limit=True if valid else None,
        closed_at_limit=True if valid else None,
        first_seal_time="10:00" if valid else None,
        last_seal_time="14:59" if valid else None,
        break_count=1 if valid else None,
        reseal_count=1 if valid else None,
        sealed_minutes=40 if valid else None,
        tail_sealed_minutes=20 if valid else None,
        one_price_limit_proxy=False if valid else None,
    )


class SingleStockResearchTests(unittest.TestCase):
    def test_composes_available_d_only_audit_without_mutating_inputs(self) -> None:
        item = candidate()
        stock = single_stock()
        before_features = dict(item.features)
        audit = build_single_stock_research_snapshot(item, stock, lifecycle())

        self.assertEqual(audit.decision_effect, AUDIT_ONLY_EFFECT)
        self.assertEqual(audit.candidate_analysis.availability, AuditAvailability.AVAILABLE)
        self.assertEqual(audit.order_spec.availability, AuditAvailability.AVAILABLE)
        self.assertEqual(audit.limit_lifecycle.availability, AuditAvailability.AVAILABLE)
        self.assertEqual(audit.single_stock.hard_gate.status, HardGateStatus.PASS)
        payload = audit.to_dict()
        self.assertEqual(
            payload["candidate_analysis"]["payload"]["features"]["ret_1d"],
            {"value": 0.0, "missing_reason": None},
        )
        self.assertEqual(item.features, before_features)

    def test_missing_values_are_null_with_reason_and_never_zero_filled(self) -> None:
        item = candidate()
        del item.metrics["p_promotion"]
        audit = build_single_stock_research_snapshot(item, single_stock(), lifecycle())
        field = audit.to_dict()["candidate_analysis"]["payload"]["metrics"][
            "p_promotion"
        ]

        self.assertIsNone(field["value"])
        self.assertIn("is missing", field["missing_reason"])

    def test_missing_order_spec_is_unavailable_with_no_payload(self) -> None:
        item = candidate()
        item.order_spec = {}
        audit = build_single_stock_research_snapshot(item, single_stock(), lifecycle())

        self.assertEqual(audit.order_spec.availability, AuditAvailability.UNAVAILABLE)
        payload = audit.to_dict()["order_spec"]
        self.assertIsNone(payload["payload"])
        self.assertIn("missing", payload["unavailable_reason"])

    def test_future_candidate_features_are_rejected(self) -> None:
        item = candidate()
        item.features["feature_asof_date"] = "20260810"
        with self.assertRaisesRegex(ContractError, "future evidence is forbidden"):
            build_single_stock_research_snapshot(item, single_stock(), lifecycle())

    def test_invalid_lifecycle_only_becomes_unavailable_and_does_not_change_gate(self) -> None:
        stock = single_stock()
        audit = build_single_stock_research_snapshot(
            candidate(),
            stock,
            lifecycle(valid=False),
        )

        self.assertEqual(audit.limit_lifecycle.availability, AuditAvailability.UNAVAILABLE)
        self.assertIsNone(audit.limit_lifecycle.payload)
        self.assertIn("minute_coverage_incomplete", audit.limit_lifecycle.unavailable_reason or "")
        self.assertEqual(audit.single_stock.hard_gate, stock.hard_gate)

    def test_wrong_date_lifecycle_is_unavailable_not_an_exception(self) -> None:
        audit = build_single_stock_research_snapshot(
            candidate(),
            single_stock(),
            lifecycle(day="20260806"),
        )
        self.assertEqual(audit.limit_lifecycle.availability, AuditAvailability.UNAVAILABLE)

    def test_snapshot_is_deeply_immutable_and_rank_mismatch_is_rejected(self) -> None:
        audit = build_single_stock_research_snapshot(
            candidate(), single_stock(), lifecycle()
        )
        with self.assertRaises(TypeError):
            audit.candidate_analysis.payload["features"] = {}  # type: ignore[index]

        item = candidate()
        item.source_ranks[SOURCE_A] = 1
        with self.assertRaisesRegex(ContractError, "source ranks disagree"):
            build_single_stock_research_snapshot(item, single_stock(), lifecycle())


if __name__ == "__main__":
    unittest.main()
