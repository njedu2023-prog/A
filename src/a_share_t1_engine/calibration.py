from __future__ import annotations

import copy
import csv
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .config import load_config


ROUTE_LABELS = {
    "height_1_to_2": "1进2",
    "height_2_to_3": "2进3",
    "height_3_to_4": "3进4",
    "height_4_to_5": "4进5",
    "n_high_continuation": "N字高标续强",
}


def apply_calibration(config: dict[str, Any], calibration_path: str | Path | None) -> dict[str, Any]:
    if calibration_path is None:
        return config
    path = Path(calibration_path)
    if not path.exists():
        return config
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    calibrated = payload.get("base_probabilities", {})
    if not isinstance(calibrated, dict):
        return config

    merged = copy.deepcopy(config)
    merged["base_probability_priors"] = copy.deepcopy(config["base_probabilities"])
    for route, value in calibrated.items():
        if route in merged["base_probabilities"]:
            merged["base_probabilities"][route] = round(float(value), int(merged["engine"]["probability_precision"]))
    merged["calibration"] = payload.get("calibration", {})
    merged["calibration"]["source_path"] = str(path)
    return merged


def calibrate_from_history(
    history_path: str | Path,
    output_path: str | Path,
    config_path: str | Path | None = None,
    prior_strength: float | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    settings = config.get("calibration", {})
    strength = float(prior_strength if prior_strength is not None else settings.get("prior_strength", 20.0))
    history = _read_history(Path(history_path))
    base = config["base_probabilities"]
    precision = int(config["engine"]["probability_precision"])

    stats = {}
    calibrated = {}
    for route, prior_probability in base.items():
        rows = [row for row in history if row.get("route") == route and row.get("top_n") == "1"]
        samples = len(rows)
        hits = sum(1 for row in rows if row.get("continued") == "1")
        prior_rate = float(prior_probability) / 100.0
        posterior_rate = (hits + strength * prior_rate) / (samples + strength) if samples + strength > 0 else prior_rate
        calibrated_probability = round(posterior_rate * 100.0, precision)
        calibrated[route] = calibrated_probability
        stats[route] = {
            "label": ROUTE_LABELS.get(route, route),
            "samples": samples,
            "hits": hits,
            "hit_rate": round(hits / samples * 100.0, precision) if samples else 0.0,
            "prior_probability": round(float(prior_probability), precision),
            "calibrated_probability": calibrated_probability,
        }

    output = {
        "calibration": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "method": "bayesian_smoothing",
            "prior_strength": strength,
            "history_path": str(history_path),
            "note": "冷启动先验经验证样本贝叶斯平滑后的基础概率；样本不足时不会大幅偏离先验。",
        },
        "base_probabilities": calibrated,
        "route_stats": stats,
    }
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.safe_dump(output, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return output


def probability_basis_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
    priors = config.get("base_probability_priors", config["base_probabilities"])
    active = config["base_probabilities"]
    rows = []
    for route, probability in active.items():
        rows.append(
            {
                "route": route,
                "label": ROUTE_LABELS.get(route, route),
                "prior_probability": float(priors.get(route, probability)),
                "active_probability": float(probability),
            }
        )
    return rows


def _read_history(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))
