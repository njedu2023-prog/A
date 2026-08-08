from __future__ import annotations

import copy
import unittest
from dataclasses import FrozenInstanceError, replace

from three_table_quant.limit_lifecycle import (
    EXPECTED_SESSION_MINUTES,
    MINUTE_CLOSE_PROXY,
    build_limit_lifecycle,
    validate_serialized_limit_lifecycle,
)
from three_table_quant.market import Bar


DAY = "20260807"
LIMIT_PRICE = 10.0


def minute_bar(
    time: str,
    *,
    day: str = DAY,
    open_price: float = 9.9,
    close: float = 9.9,
    high: float = 9.9,
    low: float = 9.9,
) -> Bar:
    return Bar(
        date=day,
        time=time,
        open=open_price,
        close=close,
        high=high,
        low=low,
        volume=1000.0,
        amount=9900.0,
        volume_unit="SHARE",
        time_semantics="INTERVAL_START",
        provider="TEST",
        price_adjustment="NONE",
        price_tick=0.01,
    )


def full_day(*, sealed_times: set[str] | None = None) -> list[Bar]:
    sealed_times = sealed_times or set()
    return [
        minute_bar(
            time,
            open_price=LIMIT_PRICE if time in sealed_times else 9.9,
            close=LIMIT_PRICE if time in sealed_times else 9.9,
            high=LIMIT_PRICE if time in sealed_times else 9.9,
            low=LIMIT_PRICE if time in sealed_times else 9.9,
        )
        for time in EXPECTED_SESSION_MINUTES
    ]


def serialized_lifecycle(*, sealed_times: set[str] | None = None) -> dict[str, object]:
    snapshot = build_limit_lifecycle(
        full_day(sealed_times=sealed_times),
        DAY,
        LIMIT_PRICE,
    )
    if not snapshot.valid:  # pragma: no cover - fixture contract
        raise AssertionError(snapshot.invalid_reasons)
    return snapshot.to_dict()


class LimitLifecycleTests(unittest.TestCase):
    def test_complete_lifecycle_counts_breaks_reseals_and_tail_minutes(self) -> None:
        sealed = {
            *(f"10:{minute:02d}" for minute in range(0, 10)),
            *(f"10:{minute:02d}" for minute in range(15, 20)),
            *(f"14:{minute:02d}" for minute in range(45, 60)),
        }

        snapshot = build_limit_lifecycle(full_day(sealed_times=sealed), DAY, LIMIT_PRICE)

        self.assertTrue(snapshot.valid)
        self.assertEqual(snapshot.evidence_level, MINUTE_CLOSE_PROXY)
        self.assertTrue(snapshot.touched_limit)
        self.assertTrue(snapshot.closed_at_limit)
        self.assertEqual(snapshot.first_seal_time, "10:00")
        self.assertEqual(snapshot.last_seal_time, "14:59")
        self.assertEqual(snapshot.break_count, 2)
        self.assertEqual(snapshot.reseal_count, 2)
        self.assertEqual(snapshot.sealed_minutes, 30)
        self.assertEqual(snapshot.tail_sealed_minutes, 15)
        self.assertFalse(snapshot.one_price_limit_proxy)
        self.assertEqual(snapshot.expected_minutes, 240)
        self.assertEqual(snapshot.observed_minutes, 240)
        self.assertEqual(snapshot.coverage_ratio, 1.0)

    def test_intraminute_touch_is_not_misreported_as_a_close_seal(self) -> None:
        bars = full_day()
        index = EXPECTED_SESSION_MINUTES.index("10:00")
        bars[index] = replace(bars[index], high=LIMIT_PRICE)

        snapshot = build_limit_lifecycle(bars, DAY, LIMIT_PRICE)

        self.assertTrue(snapshot.valid)
        self.assertTrue(snapshot.touched_limit)
        self.assertFalse(snapshot.closed_at_limit)
        self.assertIsNone(snapshot.first_seal_time)
        self.assertIsNone(snapshot.last_seal_time)
        self.assertEqual(snapshot.break_count, 0)
        self.assertEqual(snapshot.reseal_count, 0)
        self.assertEqual(snapshot.sealed_minutes, 0)
        self.assertEqual(snapshot.tail_sealed_minutes, 0)

    def test_full_session_at_limit_is_an_explicit_one_price_proxy(self) -> None:
        bars = full_day(sealed_times=set(EXPECTED_SESSION_MINUTES))

        snapshot = build_limit_lifecycle(bars, DAY, LIMIT_PRICE)

        self.assertTrue(snapshot.valid)
        self.assertTrue(snapshot.one_price_limit_proxy)
        self.assertEqual(snapshot.sealed_minutes, 240)
        self.assertEqual(snapshot.tail_sealed_minutes, 30)
        self.assertEqual(snapshot.break_count, 0)
        self.assertEqual(snapshot.reseal_count, 0)

    def test_incomplete_coverage_nulls_lifecycle_instead_of_emitting_zeros(self) -> None:
        bars = full_day()
        bars.pop()

        snapshot = build_limit_lifecycle(bars, DAY, LIMIT_PRICE)

        self.assertFalse(snapshot.valid)
        self.assertIn("minute_coverage_incomplete", snapshot.invalid_reasons)
        self.assertEqual(snapshot.observed_minutes, 239)
        self.assertAlmostEqual(snapshot.coverage_ratio or 0.0, 239 / 240)
        self.assertIsNone(snapshot.touched_limit)
        self.assertIsNone(snapshot.closed_at_limit)
        self.assertIsNone(snapshot.break_count)
        self.assertIsNone(snapshot.reseal_count)
        self.assertIsNone(snapshot.sealed_minutes)
        self.assertIsNone(snapshot.tail_sealed_minutes)
        self.assertIsNone(snapshot.one_price_limit_proxy)

    def test_tencent_five_minute_exit_fallback_cannot_claim_a_lifecycle(self) -> None:
        bars = [
            replace(minute_bar(time), provider="TENCENT")
            for time in ("11:00", "11:01", "11:02", "11:03", "11:04")
        ]

        snapshot = build_limit_lifecycle(bars, DAY, LIMIT_PRICE)

        self.assertFalse(snapshot.valid)
        self.assertEqual(snapshot.observed_minutes, 5)
        self.assertAlmostEqual(snapshot.coverage_ratio or 0.0, 5 / 240)
        self.assertIn("minute_coverage_incomplete", snapshot.invalid_reasons)
        self.assertIsNone(snapshot.touched_limit)
        self.assertIsNone(snapshot.closed_at_limit)
        self.assertIsNone(snapshot.break_count)
        self.assertIsNone(snapshot.sealed_minutes)

    def test_adjusted_minute_bar_fails_closed_with_complete_coverage(self) -> None:
        bars = full_day()
        bars[1] = replace(bars[1], price_adjustment="QFQ")

        snapshot = build_limit_lifecycle(bars, DAY, LIMIT_PRICE)

        self.assertFalse(snapshot.valid)
        self.assertEqual(snapshot.observed_minutes, 240)
        self.assertIn("adjusted_minute_bar_present", snapshot.invalid_reasons)
        self.assertIsNone(snapshot.sealed_minutes)

    def test_cross_day_bars_fail_closed(self) -> None:
        for foreign_day, expected_reason in (
            ("20260806", "non_decision_date_bar_present"),
            ("20260808", "future_bar_present"),
        ):
            with self.subTest(foreign_day=foreign_day):
                bars = full_day()
                bars.append(replace(bars[-1], date=foreign_day))

                snapshot = build_limit_lifecycle(bars, DAY, LIMIT_PRICE)

                self.assertFalse(snapshot.valid)
                self.assertIn(expected_reason, snapshot.invalid_reasons)
                self.assertIn(
                    "non_decision_date_bar_present", snapshot.invalid_reasons
                )
                self.assertIsNone(snapshot.touched_limit)

    def test_duplicate_minute_bar_fails_closed(self) -> None:
        bars = full_day()
        bars.append(bars[-1])

        snapshot = build_limit_lifecycle(bars, DAY, LIMIT_PRICE)

        self.assertFalse(snapshot.valid)
        self.assertIn("duplicate_minute_bar", snapshot.invalid_reasons)
        self.assertIsNone(snapshot.break_count)

    def test_out_of_order_minute_bars_fail_closed(self) -> None:
        bars = full_day()
        bars[0], bars[1] = bars[1], bars[0]

        snapshot = build_limit_lifecycle(bars, DAY, LIMIT_PRICE)

        self.assertFalse(snapshot.valid)
        self.assertIn("bars_not_chronological", snapshot.invalid_reasons)
        self.assertIsNone(snapshot.sealed_minutes)

    def test_price_above_frozen_limit_fails_closed(self) -> None:
        bars = full_day()
        index = EXPECTED_SESSION_MINUTES.index("10:00")
        bars[index] = replace(
            bars[index],
            close=10.01,
            high=10.01,
        )

        snapshot = build_limit_lifecycle(bars, DAY, LIMIT_PRICE)

        self.assertFalse(snapshot.valid)
        self.assertIn("price_above_limit", snapshot.invalid_reasons)
        self.assertIsNone(snapshot.touched_limit)
        self.assertIsNone(snapshot.closed_at_limit)
        self.assertIsNone(snapshot.sealed_minutes)

    def test_snapshot_is_immutable_and_serializes_reasons_as_json_shape(self) -> None:
        snapshot = build_limit_lifecycle(full_day(), DAY, LIMIT_PRICE)
        with self.assertRaises(FrozenInstanceError):
            snapshot.valid = False  # type: ignore[misc]
        self.assertEqual(snapshot.to_dict()["invalid_reasons"], [])


class SerializedLimitLifecycleValidationTests(unittest.TestCase):
    def test_accepts_exact_minute_close_proxy_and_expected_price_cross_checks(self) -> None:
        payload = serialized_lifecycle()

        result = validate_serialized_limit_lifecycle(
            payload,
            expected_decision_date=DAY,
            expected_limit_price=LIMIT_PRICE,
            expected_price_tick=0.01,
        )

        self.assertIsNone(result)

    def test_rejects_schema_evidence_validity_date_and_coverage_mutations(self) -> None:
        mutations = {
            "extra field": ("extra", True),
            "schema": ("schema_version", "limit_lifecycle_v0"),
            "evidence": ("evidence_level", "ORDER_BOOK_SEAL"),
            "valid bool": ("valid", 1),
            "invalid reasons": ("invalid_reasons", ["fabricated"]),
            "date": ("decision_date", "20260808"),
            "expected minutes": ("expected_minutes", 239),
            "observed minutes": ("observed_minutes", 239),
            "coverage": ("coverage_ratio", 0.999),
        }
        for label, (field_name, value) in mutations.items():
            with self.subTest(label=label):
                payload = serialized_lifecycle()
                payload[field_name] = value
                with self.assertRaises(ValueError):
                    validate_serialized_limit_lifecycle(
                        payload,
                        expected_decision_date=DAY,
                    )

        missing = serialized_lifecycle()
        missing.pop("coverage_ratio")
        with self.assertRaisesRegex(ValueError, "fields do not match"):
            validate_serialized_limit_lifecycle(
                missing,
                expected_decision_date=DAY,
            )

    def test_rejects_invalid_or_mismatched_limit_price_and_tick(self) -> None:
        for field_name, value in (
            ("limit_price", 0.0),
            ("price_tick", 0.0),
            ("limit_price", 10.005),
        ):
            with self.subTest(field_name=field_name, value=value):
                payload = serialized_lifecycle()
                payload[field_name] = value
                with self.assertRaises(ValueError):
                    validate_serialized_limit_lifecycle(
                        payload,
                        expected_decision_date=DAY,
                    )

        with self.assertRaisesRegex(ValueError, "limit_price mismatch"):
            validate_serialized_limit_lifecycle(
                serialized_lifecycle(),
                expected_decision_date=DAY,
                expected_limit_price=10.01,
            )
        with self.assertRaisesRegex(ValueError, "price_tick mismatch"):
            validate_serialized_limit_lifecycle(
                serialized_lifecycle(),
                expected_decision_date=DAY,
                expected_price_tick=0.001,
            )

    def test_boolean_fields_are_strict_booleans(self) -> None:
        for field_name in (
            "touched_limit",
            "closed_at_limit",
            "one_price_limit_proxy",
        ):
            with self.subTest(field_name=field_name):
                payload = serialized_lifecycle()
                payload[field_name] = 0
                with self.assertRaisesRegex(ValueError, "must be boolean"):
                    validate_serialized_limit_lifecycle(
                        payload,
                        expected_decision_date=DAY,
                    )

    def test_counts_reject_booleans_ranges_and_cross_field_inconsistency(self) -> None:
        for field_name, value in (
            ("break_count", False),
            ("reseal_count", -1),
            ("sealed_minutes", 241),
            ("tail_sealed_minutes", 1.5),
        ):
            with self.subTest(field_name=field_name, value=value):
                payload = serialized_lifecycle()
                payload[field_name] = value
                with self.assertRaises(ValueError):
                    validate_serialized_limit_lifecycle(
                        payload,
                        expected_decision_date=DAY,
                    )

        payload = serialized_lifecycle(sealed_times={"10:00"})
        payload["tail_sealed_minutes"] = 2
        with self.assertRaisesRegex(ValueError, "cannot exceed sealed_minutes"):
            validate_serialized_limit_lifecycle(
                payload,
                expected_decision_date=DAY,
            )

        payload = serialized_lifecycle(sealed_times={"10:00"})
        payload["reseal_count"] = 2
        with self.assertRaisesRegex(ValueError, "cannot exceed break_count"):
            validate_serialized_limit_lifecycle(
                payload,
                expected_decision_date=DAY,
            )

    def test_seal_times_must_be_session_minutes_in_chronological_order(self) -> None:
        for field_name, value in (
            ("first_seal_time", "09:25"),
            ("last_seal_time", "15:00"),
        ):
            with self.subTest(field_name=field_name):
                payload = serialized_lifecycle(sealed_times={"10:00"})
                payload[field_name] = value
                with self.assertRaisesRegex(ValueError, "session minute or null"):
                    validate_serialized_limit_lifecycle(
                        payload,
                        expected_decision_date=DAY,
                    )

        payload = serialized_lifecycle(sealed_times={"10:00"})
        payload["first_seal_time"] = "14:00"
        payload["last_seal_time"] = "10:00"
        with self.assertRaisesRegex(ValueError, "cannot be later"):
            validate_serialized_limit_lifecycle(
                payload,
                expected_decision_date=DAY,
            )

        payload = serialized_lifecycle(sealed_times={"10:00"})
        payload["last_seal_time"] = None
        with self.assertRaisesRegex(ValueError, "both exist or both be null"):
            validate_serialized_limit_lifecycle(
                payload,
                expected_decision_date=DAY,
            )

    def test_zero_sealed_minutes_require_null_times_false_close_and_zero_transitions(self) -> None:
        for field_name, value in (
            ("first_seal_time", "10:00"),
            ("last_seal_time", "10:00"),
            ("closed_at_limit", True),
            ("break_count", 1),
            ("reseal_count", 1),
        ):
            with self.subTest(field_name=field_name):
                payload = serialized_lifecycle()
                payload[field_name] = value
                with self.assertRaises(ValueError):
                    validate_serialized_limit_lifecycle(
                        payload,
                        expected_decision_date=DAY,
                    )

        payload = serialized_lifecycle(sealed_times={"10:00"})
        payload["first_seal_time"] = None
        payload["last_seal_time"] = None
        with self.assertRaisesRegex(ValueError, "positive sealed_minutes"):
            validate_serialized_limit_lifecycle(
                payload,
                expected_decision_date=DAY,
            )

    def test_one_price_proxy_requires_full_session_close_proxy_invariants(self) -> None:
        one_price = serialized_lifecycle(
            sealed_times=set(EXPECTED_SESSION_MINUTES)
        )
        validate_serialized_limit_lifecycle(
            one_price,
            expected_decision_date=DAY,
        )

        mutations = {
            "sealed_minutes": 239,
            "first_seal_time": "09:31",
            "last_seal_time": "14:58",
            "closed_at_limit": False,
            "touched_limit": False,
            "break_count": 1,
            "reseal_count": 1,
        }
        for field_name, value in mutations.items():
            with self.subTest(field_name=field_name):
                payload = copy.deepcopy(one_price)
                payload[field_name] = value
                with self.assertRaises(ValueError):
                    validate_serialized_limit_lifecycle(
                        payload,
                        expected_decision_date=DAY,
                    )


if __name__ == "__main__":
    unittest.main()
