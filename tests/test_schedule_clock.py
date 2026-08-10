from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from three_table_quant.domain import ContractError
from three_table_quant.schedule_clock import wait_until_not_before


SHANGHAI = ZoneInfo("Asia/Shanghai")


class ScheduleClockTests(unittest.TestCase):
    def test_before_target_sleeps_only_the_required_interval(self) -> None:
        sleeper = Mock()
        result = wait_until_not_before(
            "Asia/Shanghai",
            "20260810",
            "21:30",
            120,
            now=datetime(2026, 8, 10, 21, 29, tzinfo=SHANGHAI),
            sleeper=sleeper,
        )
        sleeper.assert_called_once_with(60.0)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["waited_seconds"], 60.0)

    def test_at_or_after_target_passes_without_sleeping(self) -> None:
        for now in (
            datetime(2026, 8, 10, 21, 30, tzinfo=SHANGHAI),
            datetime(2026, 8, 10, 22, 45, tzinfo=SHANGHAI),
        ):
            with self.subTest(now=now):
                sleeper = Mock()
                result = wait_until_not_before(
                    "Asia/Shanghai",
                    "20260810",
                    "21:30",
                    0,
                    now=now,
                    sleeper=sleeper,
                )
                sleeper.assert_not_called()
                self.assertEqual(result["waited_seconds"], 0.0)

    def test_aware_utc_clock_is_compared_in_configured_timezone(self) -> None:
        sleeper = Mock()
        wait_until_not_before(
            "Asia/Shanghai",
            "20260810",
            "21:30",
            60,
            now=datetime(2026, 8, 10, 13, 29, tzinfo=timezone.utc),
            sleeper=sleeper,
        )
        sleeper.assert_called_once_with(60.0)

    def test_cross_date_fails_closed_in_both_directions(self) -> None:
        for now in (
            datetime(2026, 8, 9, 23, 59, tzinfo=SHANGHAI),
            datetime(2026, 8, 11, 0, 1, tzinfo=SHANGHAI),
        ):
            with self.subTest(now=now), self.assertRaisesRegex(
                ContractError, "cross-date"
            ):
                wait_until_not_before(
                    "Asia/Shanghai",
                    "20260810",
                    "21:30",
                    100000,
                    now=now,
                    sleeper=Mock(),
                )

    def test_wait_beyond_budget_fails_before_sleep(self) -> None:
        sleeper = Mock()
        with self.assertRaisesRegex(ContractError, "exceeds max_wait_seconds"):
            wait_until_not_before(
                "Asia/Shanghai",
                "20260810",
                "21:30",
                59,
                now=datetime(2026, 8, 10, 21, 29, tzinfo=SHANGHAI),
                sleeper=sleeper,
            )
        sleeper.assert_not_called()

    def test_invalid_clock_inputs_fail_closed(self) -> None:
        cases = (
            {"timezone_name": "Mars/Olympus", "market_date": "20260810", "not_before": "21:30", "max_wait_seconds": 1},
            {"timezone_name": "Asia/Shanghai", "market_date": "20260230", "not_before": "21:30", "max_wait_seconds": 1},
            {"timezone_name": "Asia/Shanghai", "market_date": "20260810", "not_before": "7:30", "max_wait_seconds": 1},
            {"timezone_name": "Asia/Shanghai", "market_date": "20260810", "not_before": "21:30", "max_wait_seconds": -1},
        )
        for arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(ContractError):
                wait_until_not_before(
                    **arguments,
                    now=datetime(2026, 8, 10, 21, 30, tzinfo=SHANGHAI),
                    sleeper=Mock(),
                )

    def test_naive_now_is_rejected(self) -> None:
        with self.assertRaisesRegex(ContractError, "include a timezone"):
            wait_until_not_before(
                "Asia/Shanghai",
                "20260810",
                "21:30",
                1,
                now=datetime(2026, 8, 10, 21, 30),
                sleeper=Mock(),
            )


if __name__ == "__main__":
    unittest.main()
