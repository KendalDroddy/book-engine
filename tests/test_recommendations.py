from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from book_engine.catalog.models import Work
from book_engine.enrichment.models import (
    Concept,
    EnrichmentAttempt,
    EnrichmentRun,
    ProviderResponse,
    WorkConceptClaim,
)
from book_engine.importing.goodreads import import_goodreads_csv
from book_engine.recommendations.models import (
    DerivationRun,
    RecommendationExplanation,
    RecommendationItem,
    RecommendationSignal,
    TasteProfileRun,
    TraitDefinition,
    WorkEmbedding,
    WorkRepresentation,
    WorkTraitValue,
)
from book_engine.recommendations.service import SIGNAL_WEIGHTS, run_validation
from book_engine.recommendations.traits import TRAIT_EXTRACTOR_VERSION
from book_engine.recommendations.types import EmbeddingBatch
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


class FixtureModelEmbeddingProvider:
    name = "fixture-model"
    model = "semantic-fixture-v1"
    dimensions = 4

    def __init__(self) -> None:
        self.calls = 0

    def embed_many(self, texts: list[str]) -> EmbeddingBatch:
        self.calls += 1
        vectors = tuple(
            (1.0, 0.0, 0.0, 0.0) if "Fiction" in text else (0.0, 1.0, 0.0, 0.0)
            for text in texts
        )
        return EmbeddingBatch(
            vectors=vectors,
            response_metadata={"model": self.model, "vector_count": len(texts)},
            usage={
                "prompt_tokens": 23,
                "total_tokens": 23,
                "estimated_cost_usd": 0.000001,
            },
        )


def test_model_embeddings_preserve_derivation_and_reuse_cache(
    db_session: Session,
) -> None:
    _prepare_library(db_session)
    provider = FixtureModelEmbeddingProvider()

    first = run_validation(db_session, provider)

    assert first.embedding_provider == "fixture-model"
    assert first.derivation_run_id is not None
    assert provider.calls == 1
    derivation = db_session.get(DerivationRun, first.derivation_run_id)
    assert derivation is not None
    assert derivation.status == "completed"
    assert derivation.usage_json["prompt_tokens"] == 23
    assert len(derivation.request_json["inputs"]) == 3
    assert all("content_hash" in item for item in derivation.request_json["inputs"])
    embeddings = db_session.scalars(
        select(WorkEmbedding).where(WorkEmbedding.provider == "fixture-model")
    ).all()
    assert len(embeddings) == 3
    assert {item.derivation_run_id for item in embeddings} == {derivation.id}

    second = run_validation(db_session, provider)

    assert second.cached_run is True
    assert second.run_id == first.run_id
    assert second.embedding_cache_hits == 3
    assert second.derivation_run_id is None
    assert provider.calls == 1


def test_semantic_traits_are_versioned_and_evidenced(db_session: Session) -> None:
    _prepare_library(db_session)
    work = db_session.get(Work, 1)
    assert work is not None
    work.description = (
        "A behind the scenes account of engineering decisions and an "
        "organizational failure in a company crisis."
    )
    db_session.commit()

    run_validation(db_session)

    traits = db_session.execute(
        select(TraitDefinition.slug, WorkTraitValue)
        .join(WorkTraitValue, WorkTraitValue.trait_id == TraitDefinition.id)
        .where(
            WorkTraitValue.work_id == 1,
            WorkTraitValue.extractor_version == TRAIT_EXTRACTOR_VERSION,
            WorkTraitValue.status == "accepted",
        )
    ).all()
    by_slug = {slug: value for slug, value in traits}
    assert {
        "human-decision-making",
        "systems-failure",
        "technical-detail",
        "insider-access",
        "business-organizational-systems",
    } <= by_slug.keys()
    assert by_slug["systems-failure"].evidence_json["matched_phrases"] == [
        "organizational failure"
    ]
    assert all(len(value.input_hash) == 64 for value in by_slug.values())
