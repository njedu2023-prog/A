from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from three_table_quant.domain import ContractError
from three_table_quant.schedule_guard import market_day_context


class ScheduleGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "timezone": "Asia/Shanghai",
            "input_contract": {
                "trading_calendar_path": "data/trading_calendar_bundle.json",
            },
        }

    def context(self, market_date: str) -> dict:
        with patch(
            "three_table_quant.schedule_guard.load_config",
            return_value=self.config,
        ):
            return market_day_context("config/system.json", market_date=market_date)

    def test_open_day_is_allowed(self) -> None:
        self.assertTrue(self.context("20260810")["is_open"])

    def test_explicit_date_relation_uses_shanghai_today(self) -> None:
        now = datetime(2026, 8, 11, 0, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
        with patch(
            "three_table_quant.schedule_guard.load_config",
            return_value=self.config,
        ):
            past = market_day_context(
                "config/system.json",
                market_date="20260810",
                now=now,
            )
            today = market_day_context(
                "config/system.json",
                market_date="20260811",
                now=now,
            )
            future = market_day_context(
                "config/system.json",
                market_date="20260812",
                now=now,
            )
        self.assertEqual(past["date_relation"], "PAST")
        self.assertEqual(today["date_relation"], "TODAY")
        self.assertEqual(future["date_relation"], "FUTURE")
        self.assertEqual(past["local_today"], "20260811")

    def test_implicit_date_is_always_today_not_recovery(self) -> None:
        now = datetime(2026, 8, 11, 0, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
        with patch(
            "three_table_quant.schedule_guard.load_config",
            return_value=self.config,
        ):
            context = market_day_context("config/system.json", now=now)
        self.assertEqual(context["market_date"], "20260811")
        self.assertEqual(context["date_relation"], "TODAY")

    def test_weekend_is_closed(self) -> None:
        self.assertFalse(self.context("20260809")["is_open"])

    def test_exchange_holiday_is_closed(self) -> None:
        self.assertFalse(self.context("20261001")["is_open"])

    def test_unsupported_calendar_year_fails_closed(self) -> None:
        with self.assertRaisesRegex(ContractError, "calendar year 2027 is not VERIFIED"):
            self.context("20270104")


if __name__ == "__main__":
    unittest.main()
