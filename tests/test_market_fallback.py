from __future__ import annotations

import json
import urllib.parse
import unittest

from three_table_quant.domain import SourceIssue
from three_table_quant.http import HttpError
from three_table_quant.market import (
    Bar,
    EastmoneyMarketData,
    MarketDataError,
    ResilientMarketData,
    TencentMarketData,
)
from three_table_quant.pipeline import _append_market_fallback_issue
from three_table_quant.pipeline import (
    _append_frozen_market_fallback_issue,
    _market_data_provenance,
)


def _daily_payload(*, key: str = "qfqday", malformed: bool = False) -> dict:
    row = (
        ["2026-08-04", "10.00", "10.05", "10.10", "10.02", "12345.000"]
        if malformed
        else ["2026-08-04", "10.00", "10.05", "10.10", "9.95", "12345.000"]
    )
    return {"code": 0, "msg": "", "data": {"sh600001": {key: [row]}}}


def _raw_daily_payload(
    *,
    date: str = "2026-08-05",
    malformed: bool = False,
) -> dict:
    row = (
        [date, "10.00", "10.05", "10.04", "9.95", "12345.000"]
        if malformed
        else [date, "10.00", "10.05", "10.10", "9.95", "12345.000"]
    )
    return {"code": 0, "msg": "", "data": {"sh600001": {"day": [row]}}}


def _minute_payloads(
    *,
    evidence_date: str = "20260805",
    volume_mismatch_at: str | None = None,
    malformed_ohlc_at: str | None = None,
) -> tuple[dict, dict, list[float]]:
    period_ends = ["1101", "1102", "1103", "1104", "1105"]
    volumes = [10.0, 12.0, 8.0, 15.0, 20.0]
    amounts = [item * 100.0 * 10.05 for item in volumes]
    cumulative_volume = 100.0
    cumulative_amount = 100000.0
    cumulative_rows = [f"1100 10.00 {cumulative_volume:.6f} {cumulative_amount:.6f}"]
    mkline_rows: list[list] = []
    for raw_time, volume, amount in zip(period_ends, volumes, amounts, strict=True):
        cumulative_volume += volume
        cumulative_amount += amount
        cumulative_rows.append(
            f"{raw_time} 10.05 {cumulative_volume:.6f} {cumulative_amount:.6f}"
        )
        mkline_volume = volume + 1.0 if raw_time == volume_mismatch_at else volume
        if raw_time == malformed_ohlc_at:
            open_price, close_price, high, low = "10.00", "10.05", "10.04", "9.95"
        else:
            open_price, close_price, high, low = "10.00", "10.05", "10.10", "10.00"
        mkline_rows.append(
            [
                f"{evidence_date}{raw_time}",
                open_price,
                close_price,
                high,
                low,
                f"{mkline_volume:.6f}",
                {},
                "0.00",
            ]
        )
    query = {
        "code": 0,
        "msg": "",
        "data": {
            "sh600001": {
                "data": {
                    "data": cumulative_rows,
                    "date": evidence_date,
                }
            }
        },
    }
    mkline = {
        "code": 0,
        "msg": "",
        "data": {"sh600001": {"m1": mkline_rows}},
    }
    return query, mkline, amounts


class RouteHttp:
    def __init__(
        self,
        *,
        daily: dict | None = None,
        raw_daily: dict | None = None,
        query: dict | None = None,
        mkline: dict | None = None,
    ) -> None:
        self.daily = daily
        self.raw_daily = raw_daily
        self.query = query
        self.mkline = mkline
        self.urls: list[str] = []

    def get_bytes(self, url: str) -> bytes:
        self.urls.append(url)
        if "/appstock/app/kline/kline" in url and self.raw_daily is not None:
            payload = self.raw_daily
        elif "/fqkline/get" in url and self.daily is not None:
            payload = self.daily
        elif "/minute/query" in url and self.query is not None:
            payload = self.query
        elif "/kline/mkline" in url and self.mkline is not None:
            payload = self.mkline
        else:
            raise AssertionError(f"unexpected URL: {url}")
        return json.dumps(payload).encode("utf-8")


class AlwaysTransportFails:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def daily_bars(self, ts_code: str, end_date: str, limit: int = 100) -> list[Bar]:
        self.calls.append((ts_code, end_date, limit))
        raise HttpError("Eastmoney empty reply")


class DailyFallback:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def daily_bars(self, ts_code: str, end_date: str, limit: int = 100) -> list[Bar]:
        self.calls.append((ts_code, end_date, limit))
        return [
            Bar(
                date=end_date,
                time=None,
                open=10.0,
                close=10.0,
                high=10.0,
                low=10.0,
                volume=100.0,
                amount=0.0,
                volume_unit="LOT",
                provider="TENCENT",
                price_adjustment="QFQ",
            )
        ]


class CapturingEastmoneyRaw(EastmoneyMarketData):
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.params: dict = {}

    def _request(self, params: dict) -> dict:
        self.params = params
        return {"data": {"klines": self.lines}}


class RawTransportFails:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def raw_daily_bar(self, ts_code: str, trade_date: str) -> Bar:
        self.calls.append((ts_code, trade_date))
        raise HttpError("raw provider unavailable")


class RawFallback:
    def __init__(self, *, date: str = "20260805", adjustment: str = "NONE") -> None:
        self.date = date
        self.adjustment = adjustment
        self.calls: list[tuple[str, str]] = []

    def raw_daily_bar(self, ts_code: str, trade_date: str) -> Bar:
        self.calls.append((ts_code, trade_date))
        return Bar(
            date=self.date,
            time=None,
            open=10.0,
            close=10.5,
            high=10.5,
            low=9.9,
            volume=100.0,
            amount=0.0,
            volume_unit="LOT",
            provider="TENCENT",
            price_adjustment=self.adjustment,
        )


class EastmoneyRawDailyTests(unittest.TestCase):
    def test_raw_daily_uses_fqt_zero_and_exact_unadjusted_bar(self) -> None:
        market = CapturingEastmoneyRaw(
            ["2026-08-05,10.00,10.50,10.50,9.95,12345,12900000,5.50,5.00,0.50,2.00"]
        )

        bar = market.raw_daily_bar("600001.SH", "20260805")

        self.assertEqual(market.params["fqt"], 0)
        self.assertEqual(market.params["beg"], "20260805")
        self.assertEqual(market.params["end"], "20260805")
        self.assertEqual(bar.date, "20260805")
        self.assertEqual(bar.close, 10.5)
        self.assertEqual(bar.previous_close, 10.0)
        self.assertEqual(bar.pct_change, 5.0)
        self.assertEqual(bar.provider, "EASTMONEY")
        self.assertEqual(bar.price_adjustment, "NONE")

    def test_raw_daily_never_substitutes_a_nearby_date(self) -> None:
        market = CapturingEastmoneyRaw(
            ["2026-08-04,10.00,10.10,10.20,9.95,12345,12000000,2.50,1.00,0.10,2.00"]
        )
        with self.assertRaisesRegex(MarketDataError, "exactly one bar for 20260805"):
            market.raw_daily_bar("600001.SH", "20260805")


class TencentMarketDataTests(unittest.TestCase):
    def test_daily_qfqday_and_day_are_both_supported_without_fabricated_amount(self) -> None:
        for key in ("qfqday", "day"):
            with self.subTest(key=key):
                http = RouteHttp(daily=_daily_payload(key=key))
                market = TencentMarketData(http)
                bars = market.daily_bars("600001.SH", "20260804", limit=30)
                self.assertEqual(len(bars), 1)
                self.assertEqual(bars[0].date, "20260804")
                self.assertEqual(bars[0].amount, 0.0)
                self.assertEqual(bars[0].volume, 12345.0)
                self.assertEqual(bars[0].provider, "TENCENT")
                self.assertEqual(
                    bars[0].price_adjustment,
                    "QFQ" if key == "qfqday" else "UNKNOWN",
                )
                param = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(http.urls[0]).query
                )["param"][0]
                self.assertEqual(param.split(",")[1], "day")
                self.assertEqual(param.split(",")[-1], "qfq")

    def test_daily_malformed_ohlc_is_not_accepted(self) -> None:
        market = TencentMarketData(RouteHttp(daily=_daily_payload(malformed=True)))
        with self.assertRaisesRegex(MarketDataError, "no valid bars"):
            market.daily_bars("600001.SH", "20260804")

    def test_raw_daily_uses_raw_day_endpoint_and_exact_unadjusted_bar(self) -> None:
        http = RouteHttp(raw_daily=_raw_daily_payload())
        bar = TencentMarketData(http).raw_daily_bar("600001.SH", "20260805")

        self.assertEqual(bar.date, "20260805")
        self.assertEqual(bar.close, 10.05)
        self.assertEqual(bar.provider, "TENCENT")
        self.assertEqual(bar.price_adjustment, "NONE")
        self.assertIn("/appstock/app/kline/kline", http.urls[0])
        self.assertNotIn("qfq", http.urls[0].lower())
        param = urllib.parse.parse_qs(
            urllib.parse.urlsplit(http.urls[0]).query
        )["param"][0]
        self.assertEqual(param.split(",")[1], "day")
        self.assertEqual(param.split(",")[2:4], ["2026-08-05", "2026-08-05"])

    def test_raw_daily_fails_closed_for_wrong_date_or_malformed_ohlc(self) -> None:
        with self.assertRaisesRegex(MarketDataError, "exactly one bar for 20260805"):
            TencentMarketData(
                RouteHttp(raw_daily=_raw_daily_payload(date="2026-08-04"))
            ).raw_daily_bar("600001.SH", "20260805")
        with self.assertRaisesRegex(MarketDataError, "OHLCV is inconsistent"):
            TencentMarketData(
                RouteHttp(raw_daily=_raw_daily_payload(malformed=True))
            ).raw_daily_bar("600001.SH", "20260805")

    def test_current_day_minute_bars_use_period_end_and_cumulative_differences(self) -> None:
        query, mkline, expected_amounts = _minute_payloads()
        http = RouteHttp(query=query, mkline=mkline)
        bars = TencentMarketData(http).minute_bars("600001.SH", "20260805")
        self.assertEqual([item.source_time for item in bars], ["11:01", "11:02", "11:03", "11:04", "11:05"])
        self.assertEqual([item.time for item in bars], ["11:00", "11:01", "11:02", "11:03", "11:04"])
        self.assertEqual([item.volume for item in bars], [10.0, 12.0, 8.0, 15.0, 20.0])
        for actual, expected in zip([item.amount for item in bars], expected_amounts, strict=True):
            self.assertAlmostEqual(actual, expected)
        self.assertTrue(all(item.price_adjustment == "NONE" for item in bars))
        minute_urls = [item for item in http.urls if "/fqkline/" not in item]
        self.assertTrue(all("qfq" not in item.lower() for item in minute_urls))

    def test_historical_minute_request_is_not_fabricated_from_mkline(self) -> None:
        query, mkline, _ = _minute_payloads(evidence_date="20260805")
        http = RouteHttp(query=query, mkline=mkline)
        with self.assertRaisesRegex(MarketDataError, "only proves 20260805"):
            TencentMarketData(http).minute_bars("600001.SH", "20260804")
        self.assertEqual(len(http.urls), 1)
        self.assertIn("/minute/query", http.urls[0])

    def test_mkline_volume_must_equal_cumulative_volume_delta(self) -> None:
        query, mkline, _ = _minute_payloads(volume_mismatch_at="1103")
        with self.assertRaisesRegex(MarketDataError, "volume mismatch at 1103"):
            TencentMarketData(RouteHttp(query=query, mkline=mkline)).minute_bars(
                "600001.SH",
                "20260805",
            )

    def test_malformed_minute_ohlc_fails_closed(self) -> None:
        query, mkline, _ = _minute_payloads(malformed_ohlc_at="1102")
        with self.assertRaisesRegex(MarketDataError, "OHLC is inconsistent at 1102"):
            TencentMarketData(RouteHttp(query=query, mkline=mkline)).minute_bars(
                "600001.SH",
                "20260805",
            )


class ResilientMarketDataTests(unittest.TestCase):
    def test_raw_daily_falls_back_and_preserves_exact_none_adjusted_contract(self) -> None:
        primary = RawTransportFails()
        fallback = RawFallback()
        market = ResilientMarketData(primary=primary, fallback=fallback)

        bar = market.raw_daily_bar("600001.SH", "20260805")

        self.assertEqual(bar.date, "20260805")
        self.assertEqual(bar.price_adjustment, "NONE")
        self.assertEqual(primary.calls, [("600001.SH", "20260805")])
        self.assertEqual(fallback.calls, [("600001.SH", "20260805")])
        self.assertEqual(market.fallback_events[0]["request_type"], "raw_daily")

    def test_raw_daily_rejects_wrong_date_and_adjusted_fallback(self) -> None:
        with self.assertRaisesRegex(MarketDataError, "exact date 20260805"):
            ResilientMarketData(
                primary=RawTransportFails(),
                fallback=RawFallback(date="20260804"),
            ).raw_daily_bar("600001.SH", "20260805")
        with self.assertRaisesRegex(MarketDataError, "price_adjustment=NONE"):
            ResilientMarketData(
                primary=RawTransportFails(),
                fallback=RawFallback(adjustment="QFQ"),
            ).raw_daily_bar("600001.SH", "20260805")

    def test_first_transport_failure_opens_process_circuit_and_aggregates_warning(self) -> None:
        primary = AlwaysTransportFails()
        fallback = DailyFallback()
        market = ResilientMarketData(primary=primary, fallback=fallback)

        market.daily_bars("600001.SH", "20260804", 30)
        market.daily_bars("600002.SH", "20260804", 30)

        self.assertTrue(market.circuit_open)
        self.assertEqual(len(primary.calls), 1)
        self.assertEqual(len(fallback.calls), 2)
        self.assertEqual(
            [item["primary_status"] for item in market.fallback_events],
            ["HttpError", "CIRCUIT_OPEN"],
        )

        issues: list[SourceIssue] = []
        _append_market_fallback_issue(issues, market)
        _append_market_fallback_issue(issues, market)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].code, "MARKET_DATA_PROVIDER_FALLBACK")
        self.assertEqual(
            issues[0].details["affected_codes"],
            ["600001.SH", "600002.SH"],
        )
        self.assertEqual(issues[0].details["event_count"], 2)
        self.assertNotIn("empty reply", json.dumps(issues[0].to_dict()))

    def test_frozen_signal_keeps_fallback_provenance_without_live_requests(self) -> None:
        bars = DailyFallback().daily_bars("600001.SH", "20260804")
        signal = {"market_data_provenance": _market_data_provenance({"600001.SH": bars})}
        issues: list[SourceIssue] = []

        _append_frozen_market_fallback_issue(issues, signal)
        _append_frozen_market_fallback_issue(issues, signal)

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].details["affected_codes"], ["600001.SH"])
        self.assertTrue(issues[0].details["persisted_with_frozen_signal"])


if __name__ == "__main__":
    unittest.main()
