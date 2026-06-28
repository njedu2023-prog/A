from __future__ import annotations

from typing import Any


def source_policy_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key, source in config.get("data_sources", {}).items():
        if not isinstance(source, dict) or not source.get("enabled", False):
            continue
        rows.append(
            {
                "key": key,
                "display_name": source.get("display_name", key),
                "access_mode": source.get("access_mode", ""),
                "allowed_for": source.get("allowed_for", []),
                "truth_priority": source.get("truth_priority", ""),
                "notes": source.get("notes", ""),
            }
        )
    return rows
