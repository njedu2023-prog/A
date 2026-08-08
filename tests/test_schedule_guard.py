from __future__ import annotations

import unittest
from unittest.mock import patch

from three_table_quant.domain import ContractError
from three_table_quant.schedule_guard import market_day_context


class ScheduleGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "timezone": "Asia/Shanghai",
            "input_contract": {
                "trading_calendar_path": "data/trading_calendar_2026.json",
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

    def test_weekend_is_closed(self) -> None:
        self.assertFalse(self.context("20260809")["is_open"])

    def test_exchange_holiday_is_closed(self) -> None:
        self.assertFalse(self.context("20261001")["is_open"])

    def test_unsupported_calendar_year_fails_closed(self) -> None:
        with self.assertRaisesRegex(ContractError, "outside supported calendar year"):
            self.context("20270104")


if __name__ == "__main__":
    unittest.main()
