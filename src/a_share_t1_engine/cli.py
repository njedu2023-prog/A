from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from .pipeline import run_engine

app = typer.Typer(no_args_is_help=True)


@app.command()
def run(
    input_pdf: Path,
    output: Optional[Path] = typer.Option(None, "--output", "-o"),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    events: Optional[Path] = typer.Option(None, "--events", "-e"),
) -> None:
    report = run_engine(input_pdf, config, events)
    if output:
        output.write_text(report, encoding="utf-8")
    else:
        typer.echo(report)
