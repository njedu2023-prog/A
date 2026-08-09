from __future__ import annotations

import unittest

from three_table_quant.domain import ContractError
from three_table_quant.single_stock_v3 import (
    BOARD_LOT_FIELD,
    D_CLOSE_FIELD,
    DELISTING_FIELD,
    MAX_ORDER_SHARES_FIELD,
    PRICE_TICK_FIELD,
    PRICING_VERIFIED_FIELD,
    SECURITY_BOARD_FIELD,
    SECURITY_PRICE_LIMIT_PCT_FIELD,
    SECURITY_PRICE_TICK_FIELD,
    ST_FIELD,
    STRICT_INTERSECTION_RULE,
    SUSPENDED_FIELD,
    TRADING_RULES_VERIFIED_FIELD,
    FactProvenance,
    HardGateStatus,
    SingleStockFact,
    SingleStockSnapshotV3,
)
from three_table_quant.sources import SOURCE_A, SOURCE_DECISION, SOURCE_PREMIUM


DECISION_ASOF = "2026-08-04T21:30:00+08:00"


def provenance(
    *,
    known_at: str = "2026-08-04T15:01:00+08:00",
    fetched_at: str = "2026-08-04T21:00:00+08:00",
) -> FactProvenance:
    return FactProvenance(
        provider="TEST",
        dataset_version="fixture-v1",
        known_at=known_at,
        fetched_at=fetched_at,
        content_sha256="a" * 64,
        revision_id="fixture-1",
    )


def fact(value: object) -> SingleStockFact:
    return SingleStockFact(value=value, provenance=provenance())


def complete_facts() -> dict[str, SingleStockFact]:
    return {
        SUSPENDED_FIELD: fact(False),
        ST_FIELD: fact(False),
        DELISTING_FIELD: fact(False),
        TRADING_RULES_VERIFIED_FIELD: fact(True),
        SECURITY_BOARD_FIELD: fact("MAIN"),
        SECURITY_PRICE_LIMIT_PCT_FIELD: fact(10.0),
        SECURITY_PRICE_TICK_FIELD: fact(0.01),
        PRICING_VERIFIED_FIELD: fact(True),
        D_CLOSE_FIELD: fact(12.34),
        PRICE_TICK_FIELD: fact(0.01),
        BOARD_LOT_FIELD: fact(100),
        MAX_ORDER_SHARES_FIELD: fact(8000),
        "research.context": SingleStockFact(
            value={"industry": "IT服务", "tags": ["2→3", "strict"]},
            provenance=provenance(),
        ),
    }


def snapshot(facts: dict[str, SingleStockFact]) -> SingleStockSnapshotV3:
    return SingleStockSnapshotV3(
        ts_code="000815.sz",
        name="美利云",
        decision_date="2026-08-04",
        decision_asof=DECISION_ASOF,
        source_ranks={
            SOURCE_A: 7,
            SOURCE_PREMIUM: 2,
            SOURCE_DECISION: 9,
        },
        facts=facts,
    )


class SingleStockV3Tests(unittest.TestCase):
    def test_complete_strict_intersection_snapshot_passes_and_is_deeply_immutable(self) -> None:
        item = snapshot(complete_facts())

        self.assertEqual(item.hard_gate.status, HardGateStatus.PASS)
        self.assertEqual(item.selection_rule, STRICT_INTERSECTION_RULE)
        self.assertEqual(item.ts_code, "000815.SZ")
        self.assertEqual(
            set(item.source_ranks),
            {SOURCE_A, SOURCE_PREMIUM, SOURCE_DECISION},
        )
        with self.assertRaises(TypeError):
            item.source_ranks[SOURCE_A] = 1  # type: ignore[index]
        with self.assertRaises(TypeError):
            item.facts[D_CLOSE_FIELD] = fact(99.0)  # type: ignore[index]
        with self.assertRaises(TypeError):
            item.facts["research.context"].value["industry"] = "changed"

    def test_future_known_at_is_rejected_instead_of_entering_d_snapshot(self) -> None:
        facts = complete_facts()
        facts[D_CLOSE_FIELD] = SingleStockFact(
            12.34,
            provenance(
                known_at="2026-08-04T21:30:01+08:00",
                fetched_at="2026-08-04T21:31:00+08:00",
            ),
        )
        with self.assertRaisesRegex(ContractError, "future information is forbidden"):
            snapshot(facts)

    def test_late_replay_cannot_masquerade_as_d_asof(self) -> None:
        with self.assertRaisesRegex(ContractError, "must fall on decision_date"):
            SingleStockSnapshotV3(
                ts_code="000815.SZ",
                name="美利云",
                decision_date="20260804",
                decision_asof="2026-08-05T09:00:00+08:00",
                source_ranks={
                    SOURCE_A: 7,
                    SOURCE_PREMIUM: 2,
                    SOURCE_DECISION: 9,
                },
                facts=complete_facts(),
            )

    def test_missing_fact_remains_null_and_yields_unknown_not_zero_or_block(self) -> None:
        facts = complete_facts()
        facts[D_CLOSE_FIELD] = SingleStockFact.missing(
            "provider returned no D close",
            provenance(),
            unit="CNY",
        )
        item = snapshot(facts)

        self.assertEqual(item.hard_gate.status, HardGateStatus.UNKNOWN)
        self.assertIn(D_CLOSE_FIELD, item.hard_gate.unknown_fields)
        payload = item.to_dict()
        self.assertIsNone(payload["facts"][D_CLOSE_FIELD]["value"])
        self.assertEqual(
            payload["facts"][D_CLOSE_FIELD]["missing_reason"],
            "provider returned no D close",
        )

    def test_known_blocker_takes_precedence_over_other_unknown_fields(self) -> None:
        facts = {
            SUSPENDED_FIELD: fact(True),
            D_CLOSE_FIELD: SingleStockFact.missing("not published", provenance()),
        }
        item = snapshot(facts)

        self.assertEqual(item.hard_gate.status, HardGateStatus.BLOCK)
        self.assertIn("SUSPENDED", item.hard_gate.reasons)
        self.assertIn(D_CLOSE_FIELD, item.hard_gate.unknown_fields)

    def test_capacity_below_one_board_lot_blocks(self) -> None:
        facts = complete_facts()
        facts[MAX_ORDER_SHARES_FIELD] = fact(99)
        item = snapshot(facts)

        self.assertEqual(item.hard_gate.status, HardGateStatus.BLOCK)
        self.assertEqual(
            item.hard_gate.reasons,
            ("CAPACITY_BELOW_ONE_BOARD_LOT",),
        )

    def test_st_security_is_a_known_blocker(self) -> None:
        facts = complete_facts()
        facts[ST_FIELD] = fact(True)

        item = snapshot(facts)

        self.assertEqual(item.hard_gate.status, HardGateStatus.BLOCK)
        self.assertIn("SPECIAL_TREATMENT", item.hard_gate.reasons)

    def test_missing_st_status_is_unknown_never_inferred_false(self) -> None:
        facts = complete_facts()
        facts[ST_FIELD] = SingleStockFact.missing(
            "POINT_IN_TIME_ST_STATUS_UNAVAILABLE",
            provenance(),
        )

        item = snapshot(facts)

        self.assertEqual(item.hard_gate.status, HardGateStatus.UNKNOWN)
        self.assertIn(ST_FIELD, item.hard_gate.unknown_fields)

    def test_snapshot_cannot_be_created_from_only_two_source_memberships(self) -> None:
        with self.assertRaisesRegex(ContractError, "exact membership in all three"):
            SingleStockSnapshotV3(
                ts_code="000815.SZ",
                name="美利云",
                decision_date="20260804",
                decision_asof=DECISION_ASOF,
                source_ranks={SOURCE_A: 7, SOURCE_PREMIUM: 2},
                facts=complete_facts(),
            )

    def test_zero_is_a_real_value_not_a_missing_sentinel(self) -> None:
        zero = SingleStockFact(value=0, provenance=provenance())
        self.assertEqual(zero.value, 0)
        self.assertIsNone(zero.missing_reason)
        with self.assertRaises(ContractError):
            SingleStockFact(value=None, provenance=provenance())


if __name__ == "__main__":
    unittest.main()
