from __future__ import annotations

import json
import math
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .domain import normalize_date, normalize_ts_code
from .http import HttpClient, HttpError


@dataclass(frozen=True)
class Bar:
    date: str
    time: str | None
    open: float
    close: float
    high: float
    low: float
    volume: float
    amount: float
    pct_change: float | None = None
    turnover: float | None = None
    volume_unit: str = "UNSPECIFIED"
    limit_down: float | None = None
    price_tick: float = 0.01
    source_time: str | None = None
    time_semantics: str = "UNSPECIFIED"
    provider: str = "UNSPECIFIED"
    price_adjustment: str = "UNSPECIFIED"
    previous_close: float | None = None


class MarketDataError(RuntimeError):
    pass


def period_end_to_interval_start(date_text: str, time_text: str) -> tuple[str, str]:
    """Normalize a provider's PERIOD_END minute label to [start, start+1m)."""

    try:
        period_end = datetime.strptime(f"{date_text} {time_text[:5]}", "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise MarketDataError(f"invalid PERIOD_END minute label: {date_text} {time_text}") from exc
    interval_start = period_end - timedelta(minutes=1)
    return interval_start.strftime("%Y%m%d"), interval_start.strftime("%H:%M")


def eastmoney_secid(ts_code: str) -> str:
    normalized = normalize_ts_code(ts_code)
    number, market = normalized.split(".")
    prefix = "1" if market == "SH" else "0"
    return f"{prefix}.{number}"


def tencent_symbol(ts_code: str) -> str:
    normalized = normalize_ts_code(ts_code)
    number, market = normalized.split(".")
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(market)
    if prefix is None:
        raise MarketDataError(f"unsupported Tencent market suffix: {market}")
    return f"{prefix}{number}"


def _float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _strict_float(value: Any, field: str, *, positive: bool = False) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise MarketDataError(f"{field} must be numeric") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        qualifier = "positive " if positive else ""
        raise MarketDataError(f"{field} must be a finite {qualifier}number")
    return result


def _strict_decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MarketDataError(f"{field} must be decimal") from exc
    if not result.is_finite():
        raise MarketDataError(f"{field} must be a finite decimal")
    return result


def _json_payload(raw: bytes, provider: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarketDataError(f"{provider} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise MarketDataError(f"{provider} returned a non-object JSON payload")
    return payload


class EastmoneyMarketData:
    """Public-data adapter used only for shadow research.

    Daily open is retained as a clearly labelled proxy when a licensed auction
    execution feed is unavailable. The adapter never upgrades a proxy to an
    official fill.
    """

    endpoint = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

    def __init__(self, http: HttpClient | Any | None = None) -> None:
        self.http = http or HttpClient()

    def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        query = urllib.parse.urlencode(params)
        raw = self.http.get_bytes(f"{self.endpoint}?{query}")
        payload = _json_payload(raw, "Eastmoney")
        if not payload.get("data") or not payload["data"].get("klines"):
            raise MarketDataError(f"market data unavailable: {payload.get('message') or 'empty klines'}")
        return payload

    def daily_bars(self, ts_code: str, end_date: str, limit: int = 100) -> list[Bar]:
        end = normalize_date(end_date, "end_date")
        payload = self._request(
            {
                "secid": eastmoney_secid(ts_code),
                "klt": 101,
                "fqt": 1,
                "lmt": max(1, limit),
                "end": end,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            }
        )
        bars: list[Bar] = []
        for line in payload["data"]["klines"]:
            parts = line.split(",")
            if len(parts) < 7:
                continue
            day = normalize_date(parts[0], "market date")
            bars.append(
                Bar(
                    date=day,
                    time=None,
                    open=_float(parts[1]),
                    close=_float(parts[2]),
                    high=_float(parts[3]),
                    low=_float(parts[4]),
                    volume=_float(parts[5]),
                    amount=_float(parts[6]),
                    pct_change=_float(parts[8]) if len(parts) > 8 else None,
                    turnover=_float(parts[10]) if len(parts) > 10 else None,
                    volume_unit="LOT",
                    provider="EASTMONEY",
                    price_adjustment="QFQ",
                )
            )
        return [bar for bar in bars if bar.date <= end]

    def raw_daily_bar(self, ts_code: str, trade_date: str) -> Bar:
        """Return one exact-date, unadjusted cash-price daily bar.

        This contract is intentionally separate from ``daily_bars`` because the
        latter is forward-adjusted feature data.  A nearest prior session must
        never be substituted when an exact T-day verification bar is absent.
        """

        day = normalize_date(trade_date, "trade_date")
        payload = self._request(
            {
                "secid": eastmoney_secid(ts_code),
                "klt": 101,
                "fqt": 0,
                "lmt": 2,
                "beg": day,
                "end": day,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            }
        )
        matches: list[Bar] = []
        for line in payload["data"]["klines"]:
            parts = line.split(",")
            if len(parts) < 7:
                continue
            try:
                bar_day = normalize_date(parts[0], "Eastmoney raw daily date")
            except Exception:
                continue
            if bar_day != day:
                continue
            open_price = _strict_float(parts[1], "Eastmoney raw daily open", positive=True)
            close_price = _strict_float(parts[2], "Eastmoney raw daily close", positive=True)
            high = _strict_float(parts[3], "Eastmoney raw daily high", positive=True)
            low = _strict_float(parts[4], "Eastmoney raw daily low", positive=True)
            volume = _strict_float(parts[5], "Eastmoney raw daily volume")
            amount = _strict_float(parts[6], "Eastmoney raw daily amount")
            if (
                volume < 0
                or amount < 0
                or high < low
                or low > min(open_price, close_price)
                or high < max(open_price, close_price)
            ):
                raise MarketDataError("Eastmoney raw daily OHLCV is inconsistent")
            pct_change = (
                _strict_float(parts[8], "Eastmoney raw daily pct_change")
                if len(parts) > 8 and str(parts[8]).strip()
                else None
            )
            previous_close = None
            if len(parts) > 9 and str(parts[9]).strip():
                change = _strict_float(parts[9], "Eastmoney raw daily change")
                previous_close = close_price - change
                if not math.isfinite(previous_close) or previous_close <= 0:
                    raise MarketDataError("Eastmoney raw daily previous close is invalid")
            turnover = (
                _strict_float(parts[10], "Eastmoney raw daily turnover")
                if len(parts) > 10 and str(parts[10]).strip()
                else None
            )
            matches.append(
                Bar(
                    date=bar_day,
                    time=None,
                    open=open_price,
                    close=close_price,
                    high=high,
                    low=low,
                    volume=volume,
                    amount=amount,
                    pct_change=pct_change,
                    turnover=turnover,
                    volume_unit="LOT",
                    provider="EASTMONEY",
                    price_adjustment="NONE",
                    previous_close=previous_close,
                )
            )
        if len(matches) != 1:
            raise MarketDataError(
                f"Eastmoney raw daily requires exactly one bar for {day}; found {len(matches)}"
            )
        return matches[0]

    def minute_bars(self, ts_code: str, trade_date: str) -> list[Bar]:
        day = normalize_date(trade_date, "trade_date")
        payload = self._request(
            {
                "secid": eastmoney_secid(ts_code),
                "klt": 1,
                # Execution cash prices must never be adjusted prices. Daily
                # features may use qfq, but minute fills always use raw quotes.
                "fqt": 0,
                "lmt": 300,
                "beg": f"{day}090000",
                "end": f"{day}150000",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            }
        )
        bars: list[Bar] = []
        for line in payload["data"]["klines"]:
            parts = line.split(",")
            if len(parts) < 7 or " " not in parts[0]:
                continue
            date_text, time_text = parts[0].split(" ", 1)
            bar_day = normalize_date(date_text, "minute date")
            if bar_day != day:
                continue
            interval_day, interval_start = period_end_to_interval_start(date_text, time_text)
            if interval_day != day:
                continue
            bars.append(
                Bar(
                    date=bar_day,
                    time=interval_start,
                    open=_float(parts[1]),
                    close=_float(parts[2]),
                    high=_float(parts[3]),
                    low=_float(parts[4]),
                    volume=_float(parts[5]),
                    amount=_float(parts[6]),
                    pct_change=_float(parts[8]) if len(parts) > 8 else None,
                    turnover=_float(parts[10]) if len(parts) > 10 else None,
                    volume_unit="LOT",
                    source_time=time_text[:5],
                    time_semantics="INTERVAL_START",
                    provider="EASTMONEY",
                    price_adjustment="NONE",
                )
            )
        return bars


class TencentMarketData:
    """Tencent read-only fallback with separate feature and execution contracts.

    QFQ daily bars are suitable for trend/risk features. Execution minutes are
    raw cash prices and are emitted only when current-day cumulative turnover
    evidence independently reconciles with every requested mkline bar.
    """

    daily_endpoint = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    raw_daily_endpoint = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
    minute_ohlc_endpoint = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
    minute_query_endpoint = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
    execution_period_ends = ("1101", "1102", "1103", "1104", "1105")

    def __init__(self, http: HttpClient | Any | None = None) -> None:
        self.http = http or HttpClient()

    def _request(self, url: str, provider: str) -> dict[str, Any]:
        payload = _json_payload(self.http.get_bytes(url), provider)
        if payload.get("code") not in (0, "0"):
            raise MarketDataError(f"{provider} rejected the request")
        return payload

    @staticmethod
    def _stock_payload(payload: dict[str, Any], symbol: str, provider: str) -> dict[str, Any]:
        data = payload.get("data")
        stock = data.get(symbol) if isinstance(data, dict) else None
        if not isinstance(stock, dict):
            raise MarketDataError(f"{provider} has no payload for {symbol}")
        return stock

    def daily_bars(self, ts_code: str, end_date: str, limit: int = 100) -> list[Bar]:
        day = normalize_date(end_date, "end_date")
        count = max(1, int(limit))
        end_dt = datetime.strptime(day, "%Y%m%d")
        start_dt = end_dt - timedelta(days=max(60, count * 3))
        symbol = tencent_symbol(ts_code)
        param = ",".join(
            (
                symbol,
                "day",
                start_dt.strftime("%Y-%m-%d"),
                end_dt.strftime("%Y-%m-%d"),
                str(count),
                "qfq",
            )
        )
        url = f"{self.daily_endpoint}?{urllib.parse.urlencode({'param': param})}"
        stock = self._stock_payload(self._request(url, "Tencent fqkline"), symbol, "Tencent fqkline")
        rows = stock.get("qfqday")
        adjustment = "QFQ"
        if not isinstance(rows, list) or not rows:
            rows = stock.get("day")
            # Some Tencent instruments expose only `day`, even when qfq was
            # requested. Do not relabel that fallback as forward-adjusted.
            adjustment = "UNKNOWN"
        if not isinstance(rows, list) or not rows:
            raise MarketDataError(f"Tencent fqkline has no qfqday/day rows for {symbol}")

        bars_by_date: dict[str, Bar] = {}
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            try:
                bar_day = normalize_date(row[0], "Tencent daily date")
                open_price = _strict_float(row[1], "Tencent daily open", positive=True)
                close_price = _strict_float(row[2], "Tencent daily close", positive=True)
                high = _strict_float(row[3], "Tencent daily high", positive=True)
                low = _strict_float(row[4], "Tencent daily low", positive=True)
                volume = _strict_float(row[5], "Tencent daily volume")
            except Exception:
                continue
            if bar_day > day or volume < 0:
                continue
            if (
                high < low
                or low > min(open_price, close_price)
                or high < max(open_price, close_price)
            ):
                continue
            bars_by_date[bar_day] = Bar(
                date=bar_day,
                time=None,
                open=open_price,
                close=close_price,
                high=high,
                low=low,
                volume=volume,
                # fqkline does not provide a stable cash-turnover field. A
                # corporate-action object may occupy row[6], so never infer it.
                amount=0.0,
                volume_unit="LOT",
                provider="TENCENT",
                price_adjustment=adjustment,
            )
        bars = [bars_by_date[item] for item in sorted(bars_by_date)]
        if not bars:
            raise MarketDataError(f"Tencent fqkline returned no valid bars for {symbol}")
        return bars[-count:]

    def raw_daily_bar(self, ts_code: str, trade_date: str) -> Bar:
        """Return Tencent's exact-date raw ``day`` record without adjustment."""

        day = normalize_date(trade_date, "trade_date")
        symbol = tencent_symbol(ts_code)
        display_day = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        param = ",".join((symbol, "day", display_day, display_day, "2"))
        url = f"{self.raw_daily_endpoint}?{urllib.parse.urlencode({'param': param})}"
        stock = self._stock_payload(
            self._request(url, "Tencent raw kline"),
            symbol,
            "Tencent raw kline",
        )
        rows = stock.get("day")
        if not isinstance(rows, list):
            raise MarketDataError(f"Tencent raw kline has no day rows for {symbol}")

        matches: list[Bar] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            try:
                bar_day = normalize_date(row[0], "Tencent raw daily date")
            except Exception:
                continue
            if bar_day != day:
                continue
            open_price = _strict_float(row[1], "Tencent raw daily open", positive=True)
            close_price = _strict_float(row[2], "Tencent raw daily close", positive=True)
            high = _strict_float(row[3], "Tencent raw daily high", positive=True)
            low = _strict_float(row[4], "Tencent raw daily low", positive=True)
            volume = _strict_float(row[5], "Tencent raw daily volume")
            if (
                volume < 0
                or high < low
                or low > min(open_price, close_price)
                or high < max(open_price, close_price)
            ):
                raise MarketDataError("Tencent raw daily OHLCV is inconsistent")
            matches.append(
                Bar(
                    date=bar_day,
                    time=None,
                    open=open_price,
                    close=close_price,
                    high=high,
                    low=low,
                    volume=volume,
                    # Tencent raw day rows do not expose a stable cash-turnover
                    # field; never invent it from a neighbouring payload slot.
                    amount=0.0,
                    volume_unit="LOT",
                    provider="TENCENT",
                    price_adjustment="NONE",
                )
            )
        if len(matches) != 1:
            raise MarketDataError(
                f"Tencent raw daily requires exactly one bar for {day}; found {len(matches)}"
            )
        return matches[0]

    def _current_cumulative(
        self,
        symbol: str,
        trade_date: str,
    ) -> dict[str, tuple[Decimal, Decimal]]:
        url = f"{self.minute_query_endpoint}?{urllib.parse.urlencode({'code': symbol})}"
        stock = self._stock_payload(
            self._request(url, "Tencent minute/query"),
            symbol,
            "Tencent minute/query",
        )
        intraday = stock.get("data")
        if not isinstance(intraday, dict):
            raise MarketDataError(f"Tencent minute/query has no intraday payload for {symbol}")
        try:
            evidence_date = normalize_date(intraday.get("date"), "Tencent minute/query date")
        except Exception as exc:
            raise MarketDataError("Tencent minute/query has an invalid evidence date") from exc
        if evidence_date != trade_date:
            raise MarketDataError(
                f"Tencent minute/query only proves {evidence_date}; requested {trade_date}"
            )
        rows = intraday.get("data")
        if not isinstance(rows, list):
            raise MarketDataError(f"Tencent minute/query has no cumulative rows for {symbol}")
        cumulative: dict[str, tuple[Decimal, Decimal]] = {}
        for row in rows:
            parts = row.split() if isinstance(row, str) else list(row) if isinstance(row, list) else []
            if len(parts) < 4:
                continue
            raw_time = str(parts[0]).strip()
            if len(raw_time) != 4 or not raw_time.isdigit():
                continue
            if raw_time in cumulative:
                raise MarketDataError(f"Tencent minute/query duplicated minute {raw_time}")
            volume = _strict_decimal(parts[2], f"Tencent cumulative volume {raw_time}")
            amount = _strict_decimal(parts[3], f"Tencent cumulative amount {raw_time}")
            if volume < 0 or amount < 0:
                raise MarketDataError(f"Tencent cumulative totals became negative at {raw_time}")
            cumulative[raw_time] = (volume, amount)
        return cumulative

    def _minute_ohlc(
        self,
        symbol: str,
        trade_date: str,
    ) -> dict[str, tuple[float, float, float, float, float]]:
        param = f"{symbol},m1,,320"
        url = f"{self.minute_ohlc_endpoint}?{urllib.parse.urlencode({'param': param})}"
        stock = self._stock_payload(
            self._request(url, "Tencent mkline"),
            symbol,
            "Tencent mkline",
        )
        rows = stock.get("m1")
        if not isinstance(rows, list):
            raise MarketDataError(f"Tencent mkline has no m1 rows for {symbol}")
        wanted = set(self.execution_period_ends)
        parsed: dict[str, tuple[float, float, float, float, float]] = {}
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            timestamp = str(row[0]).strip()
            if len(timestamp) != 12 or not timestamp.startswith(trade_date):
                continue
            raw_time = timestamp[8:]
            if raw_time not in wanted:
                continue
            if raw_time in parsed:
                raise MarketDataError(f"Tencent mkline duplicated minute {raw_time}")
            open_price = _strict_float(row[1], f"Tencent mkline open {raw_time}", positive=True)
            close_price = _strict_float(row[2], f"Tencent mkline close {raw_time}", positive=True)
            high = _strict_float(row[3], f"Tencent mkline high {raw_time}", positive=True)
            low = _strict_float(row[4], f"Tencent mkline low {raw_time}", positive=True)
            volume = _strict_float(row[5], f"Tencent mkline volume {raw_time}", positive=True)
            if (
                high < low
                or low > min(open_price, close_price)
                or high < max(open_price, close_price)
            ):
                raise MarketDataError(f"Tencent mkline OHLC is inconsistent at {raw_time}")
            parsed[raw_time] = (open_price, close_price, high, low, volume)
        return parsed

    def minute_bars(self, ts_code: str, trade_date: str) -> list[Bar]:
        day = normalize_date(trade_date, "trade_date")
        symbol = tencent_symbol(ts_code)

        # Query first: it is explicitly current-day evidence. Historical mkline
        # alone has no cash-turnover proof and must remain unverifiable.
        cumulative = self._current_cumulative(symbol, day)
        ohlc = self._minute_ohlc(symbol, day)
        missing_ohlc = [item for item in self.execution_period_ends if item not in ohlc]
        if missing_ohlc:
            raise MarketDataError(
                f"Tencent mkline lacks required PERIOD_END minutes: {','.join(missing_ohlc)}"
            )

        bars: list[Bar] = []
        display_date = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        for raw_time in self.execution_period_ends:
            previous = f"{int(raw_time) - 1:04d}"
            if previous not in cumulative or raw_time not in cumulative:
                raise MarketDataError(
                    f"Tencent minute/query cannot difference {previous}->{raw_time}"
                )
            previous_volume, previous_amount = cumulative[previous]
            current_volume, current_amount = cumulative[raw_time]
            volume = current_volume - previous_volume
            amount = current_amount - previous_amount
            open_price, close_price, high, low, mkline_volume = ohlc[raw_time]
            if volume <= 0 or amount <= 0:
                raise MarketDataError(f"Tencent cumulative delta is not positive at {raw_time}")
            if abs(volume - Decimal(str(mkline_volume))) > Decimal("0.000001"):
                raise MarketDataError(
                    f"Tencent mkline/query volume mismatch at {raw_time}"
                )
            vwap = float(amount / (volume * Decimal(100)))
            tick = 0.01
            if (
                not math.isfinite(vwap)
                or vwap < low - tick / 2.0 - 1e-9
                or vwap > high + tick / 2.0 + 1e-9
            ):
                raise MarketDataError(
                    f"Tencent amount/volume delta is inconsistent with OHLC at {raw_time}"
                )
            source_time = f"{raw_time[:2]}:{raw_time[2:]}"
            interval_day, interval_start = period_end_to_interval_start(display_date, source_time)
            if interval_day != day:
                raise MarketDataError(f"Tencent minute interval crossed the requested date at {raw_time}")
            bars.append(
                Bar(
                    date=day,
                    time=interval_start,
                    open=open_price,
                    close=close_price,
                    high=high,
                    low=low,
                    volume=float(volume),
                    amount=float(amount),
                    volume_unit="LOT",
                    source_time=source_time,
                    time_semantics="INTERVAL_START",
                    provider="TENCENT",
                    price_adjustment="NONE",
                )
            )
        return bars


class ResilientMarketData:
    """Try Eastmoney, then use Tencent with a process-local circuit breaker."""

    def __init__(
        self,
        http: HttpClient | Any | None = None,
        *,
        primary: Any | None = None,
        fallback: Any | None = None,
    ) -> None:
        client = http if http is not None else HttpClient()
        self.primary = primary if primary is not None else EastmoneyMarketData(client)
        self.fallback = fallback if fallback is not None else TencentMarketData(client)
        self._eastmoney_circuit_open = False
        self._fallback_events: list[dict[str, Any]] = []

    @property
    def circuit_open(self) -> bool:
        return self._eastmoney_circuit_open

    @property
    def fallback_events(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._fallback_events]

    @staticmethod
    def _transport_failure(exc: Exception) -> bool:
        return isinstance(exc, (HttpError, TimeoutError, OSError))

    @staticmethod
    def _error_summary(exc: Exception) -> str:
        if isinstance(exc, MarketDataError):
            return f"{type(exc).__name__}:{str(exc)[:200]}"
        return type(exc).__name__

    def _fallback_call(
        self,
        method: str,
        ts_code: str,
        requested_date: str,
        args: tuple[Any, ...],
    ) -> Any:
        primary_error: Exception | None = None
        primary_status = "CIRCUIT_OPEN"
        if not self._eastmoney_circuit_open:
            try:
                return getattr(self.primary, method)(ts_code, *args)
            except Exception as exc:
                primary_error = exc
                primary_status = self._error_summary(exc)
                if self._transport_failure(exc):
                    self._eastmoney_circuit_open = True

        event = {
            "request_type": {
                "daily_bars": "daily",
                "raw_daily_bar": "raw_daily",
                "minute_bars": "minute",
            }.get(method, method),
            "ts_code": normalize_ts_code(ts_code),
            "requested_date": normalize_date(requested_date, "requested_date"),
            "primary_provider": "EASTMONEY",
            "fallback_provider": "TENCENT",
            "primary_status": primary_status,
            "fallback_status": "PENDING",
            "circuit_open": self._eastmoney_circuit_open,
        }
        try:
            result = getattr(self.fallback, method)(ts_code, *args)
        except Exception as fallback_error:
            event["fallback_status"] = self._error_summary(fallback_error)
            self._fallback_events.append(event)
            primary_chain = (
                self._error_summary(primary_error)
                if primary_error is not None
                else "CIRCUIT_OPEN"
            )
            raise MarketDataError(
                "market data providers failed "
                f"(eastmoney={primary_chain}; tencent={self._error_summary(fallback_error)})"
            ) from fallback_error
        event["fallback_status"] = "SUCCESS"
        self._fallback_events.append(event)
        return result

    def daily_bars(self, ts_code: str, end_date: str, limit: int = 100) -> list[Bar]:
        return self._fallback_call("daily_bars", ts_code, end_date, (end_date, limit))

    def raw_daily_bar(self, ts_code: str, trade_date: str) -> Bar:
        day = normalize_date(trade_date, "trade_date")
        bar = self._fallback_call("raw_daily_bar", ts_code, day, (day,))
        if not isinstance(bar, Bar):
            raise MarketDataError("raw daily provider returned a non-Bar value")
        if bar.date != day:
            raise MarketDataError(
                f"raw daily provider returned {bar.date}; exact date {day} is required"
            )
        if str(bar.price_adjustment).upper() != "NONE":
            raise MarketDataError("raw daily verification requires price_adjustment=NONE")
        return bar

    def minute_bars(self, ts_code: str, trade_date: str) -> list[Bar]:
        return self._fallback_call("minute_bars", ts_code, trade_date, (trade_date,))


def daily_bar_on(provider: Any, ts_code: str, trade_date: str) -> Bar | None:
    day = normalize_date(trade_date, "trade_date")
    bars = provider.daily_bars(ts_code, day, limit=15)
    return next((bar for bar in reversed(bars) if bar.date == day), None)


def five_minute_twap(provider: Any, ts_code: str, trade_date: str) -> tuple[float | None, list[Bar]]:
    wanted = {"11:00", "11:01", "11:02", "11:03", "11:04"}
    selected = [bar for bar in provider.minute_bars(ts_code, trade_date) if bar.time in wanted]
    selected.sort(key=lambda bar: bar.time or "")
    if {bar.time for bar in selected} != wanted:
        return None, selected
    if any(bar.amount <= 0 or bar.volume <= 0 for bar in selected):
        return None, selected
    if any(str(getattr(bar, "price_adjustment", "")).upper() != "NONE" for bar in selected):
        return None, selected
    prices: list[float] = []
    for bar in selected:
        unit = str(getattr(bar, "volume_unit", "UNSPECIFIED")).upper()
        if unit not in {"LOT", "SHARE"}:
            return None, selected
        multiplier = 100.0 if unit == "LOT" else 1.0
        value = float(bar.amount) / (float(bar.volume) * multiplier)
        tick = float(getattr(bar, "price_tick", 0.01) or 0.01)
        if (
            not math.isfinite(value)
            or value < float(bar.low) - tick / 2.0 - 1e-9
            or value > float(bar.high) + tick / 2.0 + 1e-9
        ):
            return None, selected
        prices.append(value)
    return sum(prices) / len(prices), selected
