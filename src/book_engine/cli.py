"""Command-line entry point."""

from pathlib import Path

import typer

from book_engine import __version__
from book_engine.db import SessionLocal
from book_engine.importing.goodreads import import_goodreads_csv

app = typer.Typer(no_args_is_help=True)


@app.command()
def version() -> None:
    """Print the installed Book Engine version."""
    typer.echo(__version__)


@app.command("import-goodreads")
def import_goodreads(path: Path) -> None:
    """Import a Goodreads library export into the canonical database."""
    if not path.is_file():
        raise typer.BadParameter(f"CSV file does not exist: {path}")

    with SessionLocal() as session:
        report = import_goodreads_csv(session, path)

    typer.echo(f"Import run: {report.import_run_id}")
    typer.echo(f"Total: {report.total}")
    typer.echo(f"Created: {report.created}")
    typer.echo(f"Updated: {report.updated}")
    typer.echo(f"Unchanged: {report.unchanged}")
    typer.echo(f"Ambiguous: {report.ambiguous}")
    typer.echo(f"Failed: {report.failed}")


if __name__ == "__main__":
    app()
