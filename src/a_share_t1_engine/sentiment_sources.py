from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from .models import StockRecord


def merge_sentiment_search(records: list[StockRecord], path: str | Path | None) -> None:
    if path is None:
        return
    payload = _load_payload(Path(path))
    signals = _normalize_signal_map(payload)
    by_code = {record.code: record for record in records}
    for code, signal in signals.items():
        record = by_code.get(code)
        if record is None:
            continue
        record.search_abnormal_ratio = _ratio(signal, "search")
        record.discussion_abnormal_ratio = _ratio(signal, "discussion")
        record.sentiment_direction = str(signal.get("sentiment", signal.get("direction", "neutral"))).lower()
        record.source_credibility = _bounded_float(signal.get("source_credibility", signal.get("credibility", 0.5)), 0.0, 1.0)


def _load_payload(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def _normalize_signal_map(payload: Any) -> dict[str, dict[str, Any]]:
    if isinstance(payload, dict):
        if "sentiment_search" in payload:
            return _normalize_signal_map(payload["sentiment_search"])
        return {str(code): signal if isinstance(signal, dict) else {} for code, signal in payload.items()}
    if isinstance(payload, list):
        result: dict[str, dict[str, Any]] = {}
        for item in payload:
            if isinstance(item, dict) and "code" in item:
                result[str(item["code"])] = item
        return result
    return {}


def _ratio(signal: dict[str, Any], prefix: str) -> float:
    direct_keys = (f"{prefix}_abnormal_ratio", f"{prefix}_ratio", f"{prefix}_abnormal")
    for key in direct_keys:
        if key in signal:
            return _bounded_float(signal[key], 0.0, 99.0)
    volume = _float_value(signal.get(f"{prefix}_volume", signal.get(prefix)))
    baseline = _float_value(signal.get(f"{prefix}_baseline", signal.get(f"{prefix}_avg")))
    if volume > 0 and baseline > 0:
        return _bounded_float(volume / baseline, 0.0, 99.0)
    return 1.0


def _bounded_float(value: Any, low: float, high: float) -> float:
    return max(low, min(high, _float_value(value)))


def _float_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
