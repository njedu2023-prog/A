from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from .archive import archive_html_report, archive_html_report_pages
from .pipeline import analyze_pdf, render_analysis
from .predictions import save_prediction_snapshot
from .report_pack import render_report_pack_pages

app = typer.Typer(no_args_is_help=True)


@app.command()
def run(
    input_pdf: Path,
    output: Optional[Path] = typer.Option(None, "--output", "-o"),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    events: Optional[Path] = typer.Option(None, "--events", "-e"),
    sentiment_search: Optional[Path] = typer.Option(None, "--sentiment-search", "-s"),
    ths_data: Optional[Path] = typer.Option(None, "--ths-data", help="CSV/YAML/JSON exported or transcribed from local TongHuaShun."),
    calibration: Optional[Path] = typer.Option(Path("outputs/calibration/base_probabilities.yaml"), "--calibration"),
    format: str = typer.Option("auto", "--format", "-f", help="Output format: auto, md, html, pack."),
    archive: bool = typer.Option(True, "--archive/--no-archive", help="Archive HTML reports for later review."),
    archive_dir: Path = typer.Option(Path("outputs/html_reports"), "--archive-dir", help="HTML archive directory."),
    save_prediction: bool = typer.Option(True, "--save-prediction/--no-save-prediction", help="Save machine-readable prediction snapshot."),
    prediction_dir: Path = typer.Option(Path("outputs/predictions"), "--prediction-dir", help="Prediction snapshot directory."),
) -> None:
    output_format = _resolve_output_format(format, output)
    result = analyze_pdf(input_pdf, config, events, sentiment_search, calibration, ths_data)
    report = render_analysis(result, output_format)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report, encoding="utf-8")
    if save_prediction:
        prediction_path = save_prediction_snapshot(result.scored, result.config, input_pdf, prediction_dir, result.metadata)
        typer.echo(f"Prediction snapshot saved: {prediction_path}")
    if output_format == "pack" and archive:
        archive_path = archive_html_report_pages(
            lambda base_name: render_report_pack_pages(result.scored, result.sectors, result.config, input_pdf, base_name, result.metadata),
            input_pdf,
            archive_dir,
        )
        typer.echo(f"HTML report pack saved: {archive_path}")
        typer.echo(f"HTML archive index: {archive_dir / 'index.html'}")
    elif output_format == "html" and archive:
        archive_path = archive_html_report(report, input_pdf, archive_dir)
        typer.echo(f"HTML archive saved: {archive_path}")
        typer.echo(f"HTML archive index: {archive_dir / 'index.html'}")
    if not output:
        typer.echo(report)


def _resolve_output_format(value: str, output: Optional[Path]) -> str:
    normalized = value.lower()
    if normalized == "auto":
        if output and output.suffix.lower() in {".html", ".htm"}:
            return "html"
        return "md"
    if normalized not in {"md", "html", "pack"}:
        raise typer.BadParameter("format must be one of: auto, md, html, pack")
    return normalized
