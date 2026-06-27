from __future__ import annotations

from pathlib import Path

from .config import load_config
from .event_sources import merge_external_events
from .parser import parse_report_text
from .pdf_reader import extract_text
from .report import render_report
from .scoring import score_stocks


def run_engine(
    input_pdf: str | Path,
    config_path: str | Path | None = None,
    external_events_path: str | Path | None = None,
) -> str:
    config = load_config(config_path)
    text = extract_text(input_pdf)
    stocks, sectors = parse_report_text(text, config)
    merge_external_events(stocks, external_events_path)
    scored = score_stocks(stocks, sectors, config)
    return render_report(scored, sectors, config)
