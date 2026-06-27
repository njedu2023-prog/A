from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from .models import StockRecord


def merge_external_events(records: list[StockRecord], path: str | Path | None) -> None:
    if path is None:
        return
    payload = _load_payload(Path(path))
    event_map = _normalize_event_map(payload)
    by_code = {record.code: record for record in records}
    for code, events in event_map.items():
        record = by_code.get(code)
        if record is None:
            continue
        for event in events:
            if event not in record.sensitive_events:
                record.sensitive_events.append(event)


def _load_payload(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def _normalize_event_map(payload: Any) -> dict[str, list[str]]:
    if isinstance(payload, dict):
        if "events" in payload:
            return _normalize_event_map(payload["events"])
        return {str(code): _normalize_events(events) for code, events in payload.items()}
    if isinstance(payload, list):
        result: dict[str, list[str]] = {}
        for item in payload:
            if not isinstance(item, dict) or "code" not in item:
                continue
            result[str(item["code"])] = _normalize_events(item.get("events", []))
        return result
    return {}


def _normalize_events(events: Any) -> list[str]:
    if isinstance(events, str):
        return [events]
    if isinstance(events, list):
        return [str(event) for event in events]
    return []
