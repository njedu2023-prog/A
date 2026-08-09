from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from three_table_quant.calendar import (
    CALENDAR_BUNDLE_SCHEMA,
    OFFICIAL_SSE_CALENDAR_INDEX,
    UNVERIFIED,
    VERIFIED,
    TradingCalendarBundle,
    generate_unverified_calendar_candidate,
    load_trading_calendar,
)
from three_table_quant.domain import ContractError


ROOT = Path(__file__).resolve().parents[1]
BUNDLE_PATH = ROOT / "data" / "trading_calendar_bundle.json"
OFFICIAL_FIXTURE_SOURCE = (
    "https://www.sse.com.cn/disclosure/announcement/general/"
    "c/c_20251222_10802507.shtml"
)


def annual_payload(year: int) -> dict[str, object]:
    return {
        "schema_version": "sse_trading_calendar_v1",
        "market": "SSE",
        "year": year,
        "timezone": "Asia/Shanghai",
        "weekend_days": [5, 6],
        "closed_dates": [],
        "source_title": "official fixture",
        "source_published": "2025-12-22",
        "source_url": OFFICIAL_FIXTURE_SOURCE,
    }


class CalendarBundleTests(unittest.TestCase):
    def test_verified_2026_is_available_through_the_production_bundle(self) -> None:
        calendar = load_trading_calendar(BUNDLE_PATH)
        self.assertTrue(calendar.is_open(date(2026, 8, 10)))
        self.assertFalse(calendar.is_open(date(2026, 10, 1)))

    def test_2027_metadata_candidate_is_never_treated_as_a_calendar(self) -> None:
        calendar = load_trading_calendar(BUNDLE_PATH)
        self.assertIsInstance(calendar, TradingCalendarBundle)
        self.assertEqual(calendar.entries[2027].status, UNVERIFIED)
        self.assertIsNone(calendar.candidates[2027]["closed_dates"])

        with self.assertRaisesRegex(ContractError, "not VERIFIED"):
            calendar.is_open(date(2027, 1, 4))

    def test_safe_generator_does_not_guess_open_or_closed_dates(self) -> None:
        candidate = generate_unverified_calendar_candidate(
            2027,
            checked_at="2026-08-09",
        )
        self.assertEqual(candidate["verification_status"], UNVERIFIED)
        self.assertEqual(candidate["source_discovery_url"], OFFICIAL_SSE_CALENDAR_INDEX)
        self.assertIsNone(candidate["closed_dates"])
        self.assertNotIn("open_dates", candidate)

    def test_unverified_candidate_rejects_even_an_empty_guessed_date_list(self) -> None:
        candidate = generate_unverified_calendar_candidate(
            2027,
            checked_at="2026-08-09",
        )
        candidate["closed_dates"] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "candidate.json").write_text(
                json.dumps(candidate),
                encoding="utf-8",
            )
            bundle = {
                "schema_version": CALENDAR_BUNDLE_SCHEMA,
                "market": "SSE",
                "timezone": "Asia/Shanghai",
                "years": [
                    {
                        "year": 2027,
                        "status": UNVERIFIED,
                        "path": "candidate.json",
                    }
                ],
            }
            (root / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")

            with self.assertRaisesRegex(ContractError, "must not contain guessed"):
                load_trading_calendar(root / "bundle.json")

    def test_two_verified_years_can_cross_the_year_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "2026.json").write_text(
                json.dumps(annual_payload(2026)),
                encoding="utf-8",
            )
            (root / "2027.json").write_text(
                json.dumps(annual_payload(2027)),
                encoding="utf-8",
            )
            bundle = {
                "schema_version": CALENDAR_BUNDLE_SCHEMA,
                "market": "SSE",
                "timezone": "Asia/Shanghai",
                "years": [
                    {"year": 2026, "status": VERIFIED, "path": "2026.json"},
                    {"year": 2027, "status": VERIFIED, "path": "2027.json"},
                ],
            }
            (root / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")

            calendar = load_trading_calendar(root / "bundle.json")

            self.assertEqual(
                calendar.next_open_date(date(2026, 12, 31)),
                date(2027, 1, 1),
            )
            calendar.validate_d_t_t1("20261231", "20270101", "20270104")

    def test_bundle_rejects_path_traversal_before_loading_an_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = {
                "schema_version": CALENDAR_BUNDLE_SCHEMA,
                "market": "SSE",
                "timezone": "Asia/Shanghai",
                "years": [
                    {"year": 2026, "status": VERIFIED, "path": "../outside.json"}
                ],
            }
            path = root / "bundle.json"
            path.write_text(json.dumps(bundle), encoding="utf-8")

            with self.assertRaisesRegex(ContractError, "safe relative path"):
                load_trading_calendar(path)

    def test_verified_entry_rejects_a_mismatched_annual_year(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "wrong.json").write_text(
                json.dumps(annual_payload(2025)),
                encoding="utf-8",
            )
            bundle = {
                "schema_version": CALENDAR_BUNDLE_SCHEMA,
                "market": "SSE",
                "timezone": "Asia/Shanghai",
                "years": [
                    {"year": 2026, "status": VERIFIED, "path": "wrong.json"}
                ],
            }
            path = root / "bundle.json"
            path.write_text(json.dumps(bundle), encoding="utf-8")

            with self.assertRaisesRegex(ContractError, "does not match"):
                load_trading_calendar(path)


if __name__ == "__main__":
    unittest.main()
