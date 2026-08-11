from __future__ import annotations

import json
import urllib.parse
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from three_table_quant.domain import SourceIssue
from three_table_quant.http import HttpError
from three_table_quant.market import (
    Bar,
    EastmoneyMarketData,
    MarketDataError,
    ResilientMarketData,
    TencentMarketData,
)
from three_table_quant.limit_lifecycle import EXPECTED_SESSION_MINUTES
from three_table_quant.pipeline import _append_market_fallback_issue
from three_table_quant.pipeline import (
    _append_frozen_market_fallback_issue,
    _market_data_provenance,
)


FIXTURES = Path(__file__).with_name("fixtures")


def _trends_payload() -> dict:
    return json.loads(
        (FIXTURES / "eastmoney_trends2_20260811.json").read_text(
            encoding="utf-8"
        )
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
        trends: dict | None = None,
    ) -> None:
        self.daily = daily
        self.raw_daily = raw_daily
        self.query = query
        self.mkline = mkline
        self.trends = trends
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
        elif "/trends2/get" in url and self.trends is not None:
            payload = self.trends
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


def _five_minute_bars(provider: str) -> list[Bar]:
    return [
        Bar(
            date="20260811",
            time=f"11:0{offset}",
            open=11.0,
            close=11.0,
            high=11.0,
            low=11.0,
            volume=100.0 + offset,
            amount=(100.0 + offset) * 100.0 * 11.0,
            volume_unit="LOT",
            source_time=f"11:0{offset}",
            time_semantics="INTERVAL_START",
            provider=provider,
            price_adjustment="NONE",
        )
        for offset in range(5)
    ]


class MinutePrimary:
    def __init__(
        self,
        *,
        normal_error: Exception | None = None,
        historical: list[Bar] | None = None,
    ) -> None:
        self.normal_error = normal_error
        self.historical = historical
        self.minute_calls: list[tuple[str, str]] = []
        self.historical_calls: list[tuple[str, str]] = []

    def minute_bars(self, ts_code: str, trade_date: str) -> list[Bar]:
        self.minute_calls.append((ts_code, trade_date))
        if self.normal_error is not None:
            raise self.normal_error
        return _five_minute_bars("EASTMONEY")

    def historical_minute_bars(
        self, ts_code: str, trade_date: str
    ) -> list[Bar]:
        self.historical_calls.append((ts_code, trade_date))
        if self.historical is None:
            raise MarketDataError("historical minute unavailable")
        return self.historical


class MinuteFallback:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def minute_bars(self, ts_code: str, trade_date: str) -> list[Bar]:
        self.calls.append((ts_code, trade_date))
        if self.error is not None:
            raise self.error
        return _five_minute_bars("TENCENT")


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


class EastmoneyFullSessionMinuteShapeTests(unittest.TestCase):
    @staticmethod
    def _provider_rows() -> list[str]:
        starts = (
            datetime(2026, 8, 7, 9, 31),
            datetime(2026, 8, 7, 13, 1),
        )
        counts = (120, 120)
        rows: list[str] = []
        for start, count in zip(starts, counts, strict=True):
            for offset in range(count):
                period_end = start + timedelta(minutes=offset)
                rows.append(
                    f"{period_end:%Y-%m-%d %H:%M},10.00,10.05,10.10,9.95,"
                    "1234,1240000,1.50,0.50,0.05,0.20"
                )
        return rows

    def test_realistic_period_end_session_normalizes_to_exact_240_intervals(self) -> None:
        market = CapturingEastmoneyRaw(self._provider_rows())

        bars = market.minute_bars("600001.SH", "20260807")

        self.assertEqual(market.params["fqt"], 0)
        self.assertEqual(market.params["lmt"], 300)
        self.assertEqual(len(bars), 240)
        self.assertEqual(tuple(item.time for item in bars), EXPECTED_SESSION_MINUTES)
        self.assertEqual(bars[0].source_time, "09:31")
        self.assertEqual(bars[0].time, "09:30")
        self.assertEqual(bars[119].time, "11:29")
        self.assertEqual(bars[120].source_time, "13:01")
        self.assertEqual(bars[120].time, "13:00")
        self.assertEqual(bars[-1].source_time, "15:00")
        self.assertEqual(bars[-1].time, "14:59")
        self.assertTrue(all(item.price_adjustment == "NONE" for item in bars))


class EastmoneyHistoricalTrendsTests(unittest.TestCase):
    def test_exact_window_uses_raw_per_minute_lot_and_amount_values(self) -> None:
        payload = _trends_payload()
        http = RouteHttp(trends=payload)

        bars = EastmoneyMarketData(http).historical_minute_bars(
            "002194.SZ", "20260811"
        )

        self.assertEqual(
            [item.time for item in bars],
            ["11:00", "11:01", "11:02", "11:03", "11:04"],
        )
        # These are the provider's individual minute values.  In particular,
        # the second row is not the first row subtracted from a cumulative row.
        self.assertEqual(
            [item.volume for item in bars],
            [3612.0, 2566.0, 2930.0, 1918.0, 2034.0],
        )
        self.assertEqual(
            [item.amount for item in bars],
            [3973200.07, 2822600.05, 3223012.69, 2109800.0, 2237400.0],
        )
        self.assertTrue(all(item.date == "20260811" for item in bars))
        self.assertTrue(all(item.volume_unit == "LOT" for item in bars))
        self.assertTrue(all(item.provider == "EASTMONEY_TRENDS2" for item in bars))
        self.assertTrue(all(item.price_adjustment == "NONE" for item in bars))
        self.assertTrue(all(item.time_semantics == "INTERVAL_START" for item in bars))

        params = urllib.parse.parse_qs(
            urllib.parse.urlsplit(http.urls[0]).query
        )
        self.assertEqual(params["secid"], ["0.002194"])
        self.assertEqual(params["ndays"], ["5"])
        self.assertEqual(params["fqt"], ["0"])
        self.assertEqual(params["iscr"], ["0"])
        self.assertEqual(params["iscca"], ["0"])

    def test_nearby_dates_and_1105_are_never_substituted(self) -> None:
        payload = _trends_payload()
        payload["data"]["trends"] = [
            row.replace("2026-08-11", "2026-08-10")
            for row in payload["data"]["trends"]
        ]

        with self.assertRaisesRegex(
            MarketDataError,
            "exact five target minutes; missing 11:00,11:01,11:02,11:03,11:04",
        ):
            EastmoneyMarketData(RouteHttp(trends=payload)).historical_minute_bars(
                "002194.SZ", "20260811"
            )

    def test_missing_or_duplicated_target_minute_fails_closed(self) -> None:
        missing = _trends_payload()
        missing["data"]["trends"] = [
            row
            for row in missing["data"]["trends"]
            if not row.startswith("2026-08-11 11:03,")
        ]
        with self.assertRaisesRegex(MarketDataError, "missing 11:03"):
            EastmoneyMarketData(RouteHttp(trends=missing)).historical_minute_bars(
                "002194.SZ", "20260811"
            )

        duplicated = _trends_payload()
        duplicated["data"]["trends"].append(
            duplicated["data"]["trends"][1]
        )
        with self.assertRaisesRegex(MarketDataError, "duplicated target minute 11:00"):
            EastmoneyMarketData(
                RouteHttp(trends=duplicated)
            ).historical_minute_bars("002194.SZ", "20260811")

    def test_security_identity_and_response_status_must_match(self) -> None:
        rejected = _trends_payload()
        rejected["rc"] = 1
        with self.assertRaisesRegex(MarketDataError, "rejected the request"):
            EastmoneyMarketData(RouteHttp(trends=rejected)).historical_minute_bars(
                "002194.SZ", "20260811"
            )

        wrong_code = _trends_payload()
        wrong_code["data"]["code"] = "002195"
        with self.assertRaisesRegex(MarketDataError, "different security code"):
            EastmoneyMarketData(RouteHttp(trends=wrong_code)).historical_minute_bars(
                "002194.SZ", "20260811"
            )

        wrong_market = _trends_payload()
        wrong_market["data"]["market"] = 1
        with self.assertRaisesRegex(MarketDataError, "different security market"):
            EastmoneyMarketData(
                RouteHttp(trends=wrong_market)
            ).historical_minute_bars("002194.SZ", "20260811")

    def test_ohlc_and_amount_volume_units_are_reconciled(self) -> None:
        malformed_ohlc = _trends_payload()
        parts = malformed_ohlc["data"]["trends"][1].split(",")
        parts[3] = "10.99"
        malformed_ohlc["data"]["trends"][1] = ",".join(parts)
        with self.assertRaisesRegex(MarketDataError, "OHLC is inconsistent at 11:00"):
            EastmoneyMarketData(
                RouteHttp(trends=malformed_ohlc)
            ).historical_minute_bars("002194.SZ", "20260811")

        wrong_units = _trends_payload()
        parts = wrong_units["data"]["trends"][2].split(",")
        parts[6] = "28226.0005"
        wrong_units["data"]["trends"][2] = ",".join(parts)
        with self.assertRaisesRegex(
            MarketDataError, "amount/volume is inconsistent at 11:01"
        ):
            EastmoneyMarketData(
                RouteHttp(trends=wrong_units)
            ).historical_minute_bars("002194.SZ", "20260811")


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
    def test_minute_normal_primary_success_never_touches_other_routes(self) -> None:
        primary = MinutePrimary()
        fallback = MinuteFallback()
        market = ResilientMarketData(primary=primary, fallback=fallback)

        bars = market.minute_bars("002194.SZ", "20260811")

        self.assertEqual(bars[0].provider, "EASTMONEY")
        self.assertEqual(primary.minute_calls, [("002194.SZ", "20260811")])
        self.assertEqual(primary.historical_calls, [])
        self.assertEqual(fallback.calls, [])
        self.assertEqual(market.fallback_events, [])

    def test_minute_tencent_success_precedes_historical_route(self) -> None:
        primary = MinutePrimary(normal_error=HttpError("primary unavailable"))
        fallback = MinuteFallback()
        market = ResilientMarketData(primary=primary, fallback=fallback)

        bars = market.minute_bars("002194.SZ", "20260811")

        self.assertEqual(bars[0].provider, "TENCENT")
        self.assertEqual(primary.historical_calls, [])
        self.assertEqual(fallback.calls, [("002194.SZ", "20260811")])
        self.assertEqual(market.fallback_events[0]["fallback_provider"], "TENCENT")

    def test_historical_route_is_used_only_after_both_normal_routes_fail(self) -> None:
        historical = _five_minute_bars("EASTMONEY_TRENDS2")
        primary = MinutePrimary(
            normal_error=HttpError("primary unavailable"), historical=historical
        )
        fallback = MinuteFallback(error=MarketDataError("wrong current date"))
        market = ResilientMarketData(primary=primary, fallback=fallback)

        bars = market.minute_bars("002194.SZ", "20260811")

        self.assertIs(bars, historical)
        self.assertEqual(primary.minute_calls, [("002194.SZ", "20260811")])
        self.assertEqual(fallback.calls, [("002194.SZ", "20260811")])
        self.assertEqual(primary.historical_calls, [("002194.SZ", "20260811")])
        event = market.fallback_events[0]
        self.assertEqual(event["tencent_fallback_status"], "MarketDataError:wrong current date")
        self.assertEqual(event["historical_fallback_status"], "SUCCESS")
        self.assertEqual(event["fallback_provider"], "EASTMONEY_TRENDS2")

    def test_historical_failure_is_included_and_stays_failed_closed(self) -> None:
        primary = MinutePrimary(normal_error=HttpError("primary unavailable"))
        fallback = MinuteFallback(error=MarketDataError("wrong current date"))
        market = ResilientMarketData(primary=primary, fallback=fallback)

        with self.assertRaisesRegex(
            MarketDataError,
            "tencent=MarketDataError:wrong current date; "
            "eastmoney_trends2=MarketDataError:historical minute unavailable",
        ):
            market.minute_bars("002194.SZ", "20260811")

        self.assertEqual(primary.historical_calls, [("002194.SZ", "20260811")])
        self.assertEqual(
            market.fallback_events[0]["historical_fallback_status"],
            "MarketDataError:historical minute unavailable",
        )

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
