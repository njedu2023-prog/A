from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from .validation import validate_prediction_file

app = typer.Typer(no_args_is_help=True)


@app.command()
def run(
    prediction: Path = typer.Option(Path("outputs/predictions/latest.json"), "--prediction", "-p"),
    actuals: Path = typer.Option(..., "--actuals", "-a", help="YAML file with actual continuation results."),
    output: Path = typer.Option(Path("outputs/validation/latest.html"), "--output", "-o"),
    history: Path = typer.Option(Path("outputs/validation/history.csv"), "--history"),
    archive_output: Optional[Path] = typer.Option(None, "--archive-output", help="Optional dated validation report path."),
) -> None:
    stats = validate_prediction_file(prediction, actuals, output, history)
    if archive_output:
        archive_output.parent.mkdir(parents=True, exist_ok=True)
        archive_output.write_text(output.read_text(encoding="utf-8"), encoding="utf-8")
    summary = stats["overall_top_n"]
    typer.echo(f"Validation report saved: {output}")
    typer.echo(f"TopN cumulative hit rate: {summary['hits']}/{summary['total']} = {summary['hit_rate']:.1f}%")
