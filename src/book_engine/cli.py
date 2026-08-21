"""Command-line entry point."""

from pathlib import Path

import typer

from book_engine import __version__
from book_engine.config import get_settings
from book_engine.db import SessionLocal
from book_engine.enrichment.providers.openlibrary import OpenLibraryProvider
from book_engine.enrichment.service import enrich_work
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


@app.command("enrich")
def enrich(
    work_id: int = typer.Option(..., "--work-id", min=1),
    refresh: bool = typer.Option(False, "--refresh"),
) -> None:
    """Enrich one canonical work with Open Library metadata."""
    settings = get_settings()
    provider = OpenLibraryProvider(
        contact_email=settings.openlibrary_contact_email,
        timeout_seconds=settings.openlibrary_timeout_seconds,
    )
    with SessionLocal() as session:
        try:
            report = enrich_work(session, work_id, provider, refresh=refresh)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"Enrichment run: {report.run_id}")
    typer.echo(f"Work: {report.work_id}")
    typer.echo(f"Status: {report.status}")
    if report.decision:
        typer.echo(f"Decision: {report.decision.method} - {report.decision.reason}")
        if report.decision.candidate:
            typer.echo(
                "Match: "
                f"{report.decision.candidate.external_work_id} "
                f"({report.decision.score:.4f})"
            )
        if report.decision.equivalent_candidates:
            typer.echo(
                "Equivalent provider records: "
                + ", ".join(
                    candidate.external_work_id
                    for candidate in report.decision.equivalent_candidates
                )
            )
        for evaluation in report.decision.evaluations:
            typer.echo(
                "Candidate: "
                f"{evaluation.candidate.external_work_id} | "
                f"{evaluation.candidate.title} | score={evaluation.score:.4f} | "
                f"isbn={evaluation.isbn_match} | "
                f"title={evaluation.title_similarity:.4f} | "
                f"author={evaluation.author_similarity:.4f} | "
                f"year_delta={evaluation.year_difference} | "
                f"conflicts={list(evaluation.conflicts)}"
            )
    typer.echo(f"Supplied: {', '.join(report.supplied_fields) or 'none'}")
    typer.echo(f"Accepted: {', '.join(report.accepted_fields) or 'none'}")
    typer.echo(
        f"Retained as candidates: {', '.join(report.candidate_fields) or 'none'}"
    )
    if report.error:
        typer.echo(f"Error: {report.error}")


if __name__ == "__main__":
    app()
