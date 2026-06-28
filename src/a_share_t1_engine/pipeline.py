from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .calibration import apply_calibration
from .config import load_config
from .event_sources import merge_external_events
from .models import ReportMetadata, ScoredStock, SectorRotation
from .parser import parse_report_metadata, parse_report_text
from .pdf_reader import extract_text
from .report import render_html_report, render_report
from .report_pack import render_report_pack
from .scoring import score_stocks
from .sentiment_sources import merge_sentiment_search
from .ths_sources import merge_ths_data
from .trading_calendar import enrich_report_metadata


@dataclass(frozen=True)
class AnalysisResult:
    scored: list[ScoredStock]
    sectors: list[SectorRotation]
    config: dict[str, Any]
    input_pdf: Path | None = None
    metadata: ReportMetadata = ReportMetadata()


def analyze_pdf(
    input_pdf: str | Path,
    config_path: str | Path | None = None,
    external_events_path: str | Path | None = None,
    sentiment_search_path: str | Path | None = None,
    calibration_path: str | Path | None = None,
    ths_data_path: str | Path | None = None,
) -> AnalysisResult:
    config = apply_calibration(load_config(config_path), calibration_path)
    text = extract_text(input_pdf, config)
    metadata = enrich_report_metadata(parse_report_metadata(text), input_pdf, config)
    stocks, sectors = parse_report_text(text, config)
    merge_external_events(stocks, external_events_path)
    merge_sentiment_search(stocks, sentiment_search_path)
    sectors = merge_ths_data(stocks, sectors, ths_data_path, config)
    scored = score_stocks(stocks, sectors, config)
    return AnalysisResult(scored=scored, sectors=sectors, config=config, input_pdf=Path(input_pdf), metadata=metadata)


def render_analysis(result: AnalysisResult, output_format: str = "md") -> str:
    if output_format == "pack":
        return render_report_pack(result.scored, result.sectors, result.config, result.input_pdf, result.metadata)
    if output_format == "html":
        return render_html_report(result.scored, result.sectors, result.config)
    return render_report(result.scored, result.sectors, result.config)


def run_engine(
    input_pdf: str | Path,
    config_path: str | Path | None = None,
    external_events_path: str | Path | None = None,
    output_format: str = "md",
    sentiment_search_path: str | Path | None = None,
    calibration_path: str | Path | None = None,
    ths_data_path: str | Path | None = None,
) -> str:
    return render_analysis(analyze_pdf(input_pdf, config_path, external_events_path, sentiment_search_path, calibration_path, ths_data_path), output_format)
