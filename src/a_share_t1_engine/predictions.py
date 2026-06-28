from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import ReportMetadata, ScoredStock


def save_prediction_snapshot(
    scored: list[ScoredStock],
    config: dict[str, Any],
    input_pdf: Path,
    prediction_dir: Path,
    metadata: ReportMetadata | None = None,
) -> Path:
    prediction_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prediction_id = f"{timestamp}_{_safe_name(input_pdf.stem)}"
    snapshot = build_prediction_snapshot(scored, config, input_pdf, prediction_id, timestamp, metadata)
    path = prediction_dir / f"{prediction_id}.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copyfile(path, prediction_dir / "latest.json")
    return path


def build_prediction_snapshot(
    scored: list[ScoredStock],
    config: dict[str, Any],
    input_pdf: Path,
    prediction_id: str,
    timestamp: str,
    metadata: ReportMetadata | None = None,
) -> dict[str, Any]:
    top_n = int(config["output"]["top_n"])
    engine = config["engine"]
    metadata = metadata or ReportMetadata()
    return {
        "prediction_id": prediction_id,
        "generated_at": timestamp,
        "input_pdf": str(input_pdf),
        "trade_dates": {
            "auction_buy_date": metadata.auction_buy_date,
            "auction_buy_date_iso": metadata.auction_buy_date_iso,
            "expected_sell_date": metadata.t1_sell_date,
        },
        "top_n": top_n,
        "model_versions": {
            "probability": engine["probability_model_version"],
            "policy": engine["policy_model_version"],
        },
        "candidates": [_candidate_row(item, rank, rank <= top_n) for rank, item in enumerate(scored, start=1)],
    }


def _candidate_row(item: ScoredStock, rank: int, top_n: bool) -> dict[str, Any]:
    stock = item.stock
    return {
        "rank": rank,
        "code": stock.code,
        "name": stock.name,
        "probability": item.probability,
        "ecs_grade": item.ecs_grade,
        "height": stock.height,
        "board_quality": stock.board_quality,
        "first_limit_up_time": stock.first_limit_up_time,
        "final_limit_up_time": stock.final_limit_up_time,
        "route": item.route,
        "iqs": item.iqs,
        "tss": item.tss,
        "sas": item.sas,
        "sentiment_adjustment": item.sentiment_adjustment,
        "sentiment_bucket": _sentiment_bucket(item.sas),
        "list_sources": sorted(stock.list_sources),
        "top_n": top_n,
    }


def _sentiment_bucket(sas: float) -> str:
    if sas >= 70:
        return "high"
    if sas >= 45:
        return "medium"
    return "low"


def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in value).strip("._") or "report"
