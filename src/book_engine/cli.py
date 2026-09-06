"""Command-line entry point."""

from pathlib import Path

import typer

from book_engine import __version__
from book_engine.config import get_settings
from book_engine.db import SessionLocal
from book_engine.enrichment.providers.openlibrary import OpenLibraryProvider
from book_engine.enrichment.service import enrich_work
from book_engine.importing.goodreads import import_goodreads_csv
from book_engine.web.facets import sync_browse_facets

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


@app.command("sync-browse-facets")
def sync_facets() -> None:
    """Build the reviewed browse taxonomy from preserved provider concepts."""
    with SessionLocal() as session:
        facet_count, mapping_count = sync_browse_facets(session)
    typer.echo(f"Facets: {facet_count}")
    typer.echo(f"Concept mappings: {mapping_count}")


@app.command("recommend-validate")
def recommend_validate(
    embedding_provider: str = typer.Option(
        "local", "--embedding-provider", help="local or openai"
    ),
) -> None:
    """Run the bounded positive-only recommendation validation."""
    from sqlalchemy import select

    from book_engine.catalog.models import Work
    from book_engine.recommendations.models import (
        RecommendationExplanation,
        RecommendationItem,
        RecommendationNeighbor,
        RecommendationSignal,
        TasteProfileRun,
        TasteProfileValue,
    )
    from book_engine.recommendations.providers import OpenAIEmbeddingProvider
    from book_engine.recommendations.representation import HashingEmbeddingProvider
    from book_engine.recommendations.service import run_validation
    from book_engine.recommendations.types import EmbeddingProvider

    settings = get_settings()
    provider: EmbeddingProvider
    if embedding_provider == "local":
        provider = HashingEmbeddingProvider()
    elif embedding_provider == "openai":
        if not settings.openai_api_key:
            raise typer.BadParameter(
                "BOOK_ENGINE_OPENAI_API_KEY is required for the OpenAI provider"
            )
        provider = OpenAIEmbeddingProvider(
            api_key=settings.openai_api_key,
            model=settings.openai_embedding_model,
            dimensions=settings.openai_embedding_dimensions,
            timeout_seconds=settings.openai_timeout_seconds,
        )
    else:
        raise typer.BadParameter("embedding provider must be 'local' or 'openai'")

    with SessionLocal() as session:
        report = run_validation(session, provider)
        typer.echo(f"Recommendation run: {report.run_id}")
        typer.echo(f"Taste profile: {report.profile_run_id}")
        typer.echo(f"Read works: {report.read_work_count}")
        typer.echo(f"Semantic anchors: {report.represented_read_count}")
        typer.echo(f"Validation candidates: {report.candidate_count}")
        typer.echo(f"Representation cache hits: {report.representation_cache_hits}")
        typer.echo(f"Embedding cache hits: {report.embedding_cache_hits}")
        typer.echo(f"Cached recommendation run: {report.cached_run}")
        typer.echo(
            f"Embedding provider: {report.embedding_provider}/{report.embedding_model}"
        )
        typer.echo(f"Derivation run: {report.derivation_run_id or 'cache only'}")

        typer.echo("\nTaste profile:")
        profile_values = session.scalars(
            select(TasteProfileValue)
            .where(TasteProfileValue.profile_run_id == report.profile_run_id)
            .order_by(TasteProfileValue.support_count.desc())
        ).all()
        for value in profile_values:
            typer.echo(
                f"- {value.label}: weight={value.weight:.3f}, "
                f"support={value.support_count}"
            )

        typer.echo("\nSemantic anchor books:")
        profile = session.get(TasteProfileRun, report.profile_run_id)
        assert profile is not None
        anchor_ids = profile.configuration["semantic_anchor_work_ids"]
        anchor_rows = session.execute(
            select(Work.id, Work.title).where(Work.id.in_(anchor_ids)).order_by(Work.id)
        ).all()
        for work_id, title in anchor_rows:
            typer.echo(f"- {work_id}: {title}")

        typer.echo("\nRanked validation candidates:")
        items = session.scalars(
            select(RecommendationItem)
            .where(RecommendationItem.run_id == report.run_id)
            .order_by(RecommendationItem.rank)
        ).all()
        for item in items:
            title = session.scalar(select(Work.title).where(Work.id == item.work_id))
            typer.echo(
                f"\n{item.rank}. {title} | score={item.reranked_score:.1f} "
                f"({item.match_label}) | confidence={item.confidence_label} "
                f"| repetitive={item.repetitive}"
            )
            signals = session.scalars(
                select(RecommendationSignal)
                .where(RecommendationSignal.recommendation_item_id == item.id)
                .order_by(RecommendationSignal.contribution.desc())
            ).all()
            for signal in signals:
                typer.echo(
                    f"  {signal.signal_name}: raw={signal.raw_value:.3f}, "
                    f"weight={signal.weight:.2f}, +{signal.contribution:.2f}"
                )
            neighbors = session.execute(
                select(Work.title, RecommendationNeighbor.similarity)
                .join(
                    RecommendationNeighbor,
                    RecommendationNeighbor.read_work_id == Work.id,
                )
                .where(RecommendationNeighbor.recommendation_item_id == item.id)
                .order_by(RecommendationNeighbor.rank)
            ).all()
            typer.echo(
                "  Closest reads: "
                + "; ".join(
                    f"{neighbor_title} ({similarity:.3f})"
                    for neighbor_title, similarity in neighbors
                )
            )
            explanation = session.scalar(
                select(RecommendationExplanation).where(
                    RecommendationExplanation.recommendation_item_id == item.id
                )
            )
            if explanation:
                typer.echo(f"  Why: {explanation.rendered_text}")


@app.command("discover-recommend")
def discover_recommend(
    limit: int = typer.Option(30, "--limit", min=1, max=100),
) -> None:
    """Discover and rank a bounded set of external candidates."""
    from sqlalchemy import select

    from book_engine.catalog.models import Work
    from book_engine.discovery.models import DiscoveryCandidate
    from book_engine.discovery.service import discover_candidates
    from book_engine.recommendations.models import (
        RecommendationExplanation,
        RecommendationItem,
        RecommendationNeighbor,
        RecommendationSignal,
    )
    from book_engine.recommendations.service import run_discovery_validation

    settings = get_settings()
    provider = OpenLibraryProvider(
        contact_email=settings.openlibrary_contact_email,
        timeout_seconds=settings.openlibrary_timeout_seconds,
    )
    with SessionLocal() as session:
        discovery = discover_candidates(session, provider, limit=limit)
        typer.echo(f"Discovery run: {discovery.run_id}")
        typer.echo(f"Selected: {discovery.selected_count}")
        typer.echo(f"Enriched: {discovery.enriched_count}")
        typer.echo(f"Enrichment failures: {discovery.failed_count}")
        typer.echo(f"Excluded library/duplicates: {discovery.excluded_count}")
        typer.echo(
            f"External requests this invocation: {discovery.external_request_count}"
        )
        typer.echo(f"Cached discovery: {discovery.cache_hit}")
        if discovery.selected_count == 0:
            raise typer.Exit(code=1)
        recommendation = run_discovery_validation(session, discovery.run_id)
        typer.echo(f"Recommendation run: {recommendation.run_id}")
        typer.echo(f"Cached recommendation: {recommendation.cached_run}")
        typer.echo(f"Embedding cache hits: {recommendation.embedding_cache_hits}")

        typer.echo("\nTop recommendations:")
        items = session.scalars(
            select(RecommendationItem)
            .where(RecommendationItem.run_id == recommendation.run_id)
            .order_by(RecommendationItem.rank)
            .limit(10)
        ).all()
        for item in items:
            candidate = session.scalar(
                select(DiscoveryCandidate).where(
                    DiscoveryCandidate.run_id == discovery.run_id,
                    DiscoveryCandidate.work_id == item.work_id,
                )
            )
            title = session.scalar(select(Work.title).where(Work.id == item.work_id))
            assert candidate is not None
            typer.echo(
                f"\n{item.rank}. {title} | score={item.reranked_score:.1f} "
                f"({item.match_label}) | confidence={item.confidence_label} "
                f"| repetitive={item.repetitive}"
            )
            typer.echo("  Discovered via: " + ", ".join(candidate.cluster_slugs))
            signals = session.scalars(
                select(RecommendationSignal)
                .where(RecommendationSignal.recommendation_item_id == item.id)
                .order_by(RecommendationSignal.contribution.desc())
            ).all()
            for signal in signals:
                evidence = f" | {signal.evidence_json}" if signal.evidence_json else ""
                typer.echo(
                    f"  {signal.signal_name}: raw={signal.raw_value:.3f}, "
                    f"weight={signal.weight:.2f}, +{signal.contribution:.2f}{evidence}"
                )
            neighbors = session.execute(
                select(Work.title, RecommendationNeighbor.similarity)
                .join(
                    RecommendationNeighbor,
                    RecommendationNeighbor.read_work_id == Work.id,
                )
                .where(RecommendationNeighbor.recommendation_item_id == item.id)
                .order_by(RecommendationNeighbor.rank)
            ).all()
            typer.echo(
                "  Closest reads: "
                + "; ".join(
                    f"{neighbor_title} ({similarity:.3f})"
                    for neighbor_title, similarity in neighbors
                )
            )
            explanation = session.scalar(
                select(RecommendationExplanation).where(
                    RecommendationExplanation.recommendation_item_id == item.id
                )
            )
            if explanation:
                typer.echo(f"  Why: {explanation.rendered_text}")


@app.command("reputation")
def reputation(
    discovery_run_id: int = typer.Option(1, "--discovery-run"),
) -> None:
    """Collect cached book reputation and calculate combined recommendation scores."""
    from book_engine.reputation.providers import GoogleBooksReputationProvider
    from book_engine.reputation.service import enrich_discovery_reputation

    settings = get_settings()
    provider = GoogleBooksReputationProvider(
        api_key=settings.google_books_api_key,
        timeout_seconds=settings.google_books_timeout_seconds,
    )
    with SessionLocal() as session:
        report = enrich_discovery_reputation(session, discovery_run_id, provider)
    typer.echo(f"Discovery run: {report.discovery_run_id}")
    typer.echo(f"Recommendation run: {report.recommendation_run_id}")
    typer.echo(f"Attempted: {report.attempted}")
    typer.echo(f"Succeeded: {report.succeeded}")
    typer.echo(f"Missed: {report.missed}")
    typer.echo(f"Ambiguous: {report.ambiguous}")
    typer.echo(f"Failed: {report.failed}")
    typer.echo(f"Cache hits: {report.cache_hits}")
    typer.echo(f"External requests: {report.external_requests}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port", min=1, max=65535),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """Run the local library browser."""
    import uvicorn

    uvicorn.run("book_engine.web.app:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
