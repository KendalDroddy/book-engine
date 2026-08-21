from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from book_engine.enrichment.models import (
    Concept,
    EnrichmentAttempt,
    EnrichmentRun,
    ProviderResponse,
    WorkConceptClaim,
)
from book_engine.importing.goodreads import import_goodreads_csv
from book_engine.recommendations.models import (
    RecommendationExplanation,
    RecommendationItem,
    RecommendationSignal,
    TasteProfileRun,
    WorkEmbedding,
    WorkRepresentation,
    WorkTraitValue,
)
from book_engine.recommendations.service import SIGNAL_WEIGHTS, run_validation
from book_engine.web.facets import sync_browse_facets
from book_engine.web.models import BrowseFacet, ConceptFacetMapping

FIXTURES = Path(__file__).parent / "fixtures"


def _prepare_library(session: Session) -> None:
    import_goodreads_csv(session, FIXTURES / "goodreads_sample.csv")
    concept = Concept(kind="subject", label="Fiction", normalized_label="fiction")
    session.add(concept)
    session.flush()
    sync_browse_facets(session)
    fiction = session.scalar(select(BrowseFacet).where(BrowseFacet.slug == "fiction"))
    assert fiction is not None
    mapping = session.get(ConceptFacetMapping, (concept.id, fiction.id))
    assert mapping is not None
    now = datetime(2026, 8, 21, 12, 0, 0)
    run = EnrichmentRun(
        provider="fixture",
        mode="single",
        status="completed",
        started_at=now,
        completed_at=now,
        requested_count=1,
        succeeded_count=1,
        configuration={},
    )
    session.add(run)
    session.flush()
    attempt = EnrichmentAttempt(
        run_id=run.id,
        work_id=1,
        edition_id=1,
        provider="fixture",
        lookup_strategy="fixture",
        lookup_key="fixture:1",
        status="succeeded",
        candidate_summary=[],
        started_at=now,
        completed_at=now,
    )
    session.add(attempt)
    session.flush()
    response = ProviderResponse(
        attempt_id=attempt.id,
        provider="fixture",
        operation="fetch",
        request_key="fixture:1",
        endpoint="fixture",
        retrieved_at=now,
        status_code=200,
        outcome="success",
        raw_payload={},
        payload_checksum="fixture",
        parser_version="fixture-v1",
    )
    session.add(response)
    session.flush()
    session.add(
        WorkConceptClaim(
            work_id=1,
            concept_id=concept.id,
            provider_response_id=response.id,
            original_label="Fiction",
            confidence=1.0,
            status="accepted",
        )
    )
    session.commit()


def test_validation_is_positive_only_explainable_and_cached(
    db_session: Session,
) -> None:
    _prepare_library(db_session)

    first = run_validation(db_session)

    assert first.read_work_count == 2
    assert first.represented_read_count == 2
    assert first.candidate_count == 1
    assert first.cached_run is False
    profile = db_session.get(TasteProfileRun, first.profile_run_id)
    assert profile is not None
    assert profile.configuration["ratings_used"] is False
    item = db_session.scalar(
        select(RecommendationItem).where(RecommendationItem.run_id == first.run_id)
    )
    assert item is not None
    signals = db_session.scalars(
        select(RecommendationSignal).where(
            RecommendationSignal.recommendation_item_id == item.id
        )
    ).all()
    assert {signal.signal_name for signal in signals} == set(SIGNAL_WEIGHTS)
    assert round(sum(signal.contribution for signal in signals), 3) == round(
        item.base_score, 3
    )
    explanation = db_session.scalar(
        select(RecommendationExplanation).where(
            RecommendationExplanation.recommendation_item_id == item.id
        )
    )
    assert explanation is not None
    assert explanation.generator == "deterministic"
    assert explanation.rendered_text

    representation_count = db_session.scalar(
        select(func.count()).select_from(WorkRepresentation)
    )
    embedding_count = db_session.scalar(select(func.count()).select_from(WorkEmbedding))
    second = run_validation(db_session)

    assert second.cached_run is True
    assert second.run_id == first.run_id
    assert second.representation_cache_hits == 3
    assert second.embedding_cache_hits == 3
    assert (
        db_session.scalar(select(func.count()).select_from(WorkRepresentation))
        == representation_count
    )
    assert (
        db_session.scalar(select(func.count()).select_from(WorkEmbedding))
        == embedding_count
    )


def test_derived_records_include_replaceable_provider_provenance(
    db_session: Session,
) -> None:
    _prepare_library(db_session)

    run_validation(db_session)

    embedding = db_session.scalar(select(WorkEmbedding))
    assert embedding is not None
    assert embedding.provider == "local"
    assert embedding.model == "signed-hashing-v2"
    assert len(embedding.input_hash) == 64
    trait = db_session.scalar(select(WorkTraitValue))
    assert trait is not None
    assert trait.source_kind == "deterministic"
    assert trait.source_reference["browse_facet_id"] > 0
    assert len(trait.input_hash) == 64
