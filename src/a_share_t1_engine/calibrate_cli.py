from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from .calibration import calibrate_from_history

app = typer.Typer(no_args_is_help=True)


@app.command()
def run(
    history: Path = typer.Option(Path("outputs/validation/history.csv"), "--history"),
    output: Path = typer.Option(Path("outputs/calibration/base_probabilities.yaml"), "--output", "-o"),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    prior_strength: Optional[float] = typer.Option(None, "--prior-strength"),
) -> None:
    result = calibrate_from_history(history, output, config, prior_strength)
    typer.echo(f"Calibration saved: {output}")
    for route, stats in result["route_stats"].items():
        typer.echo(
            f"{stats['label']} {route}: "
            f"prior={stats['prior_probability']:.1f}% "
            f"samples={stats['samples']} hits={stats['hits']} "
            f"calibrated={stats['calibrated_probability']:.1f}%"
        )
