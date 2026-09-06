from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from book_engine.catalog.models import Edition, Work
from book_engine.discovery.models import (
    DiscoveryCandidate,
    DiscoveryQuery,
    DiscoveryRun,
)
from book_engine.discovery.service import CLUSTER_QUERIES, discover_candidates
from book_engine.enrichment.types import (
    BookLookup,
    BookMetadata,
    MetadataCandidate,
    ProviderDiscoveryResult,
    ProviderMetadataResult,
    ProviderSearchResult,
)
from book_engine.importing.goodreads import import_goodreads_csv
from book_engine.library.models import LibraryEntry
from book_engine.recommendations.models import (
    RecommendationExplanation,
    RecommendationItem,
    RecommendationSignal,
)
from book_engine.recommendations.service import run_discovery_validation
from book_engine.web.facets import sync_browse_facets
from book_engine.web.recommendations import record_recommendation_feedback

FIXTURES = Path(__file__).parent / "fixtures"


class FixtureDiscoveryProvider:
    name = "openlibrary"

    def __init__(self) -> None:
        self.discovery_calls = 0
        self.fetch_calls = 0

    def discover(self, query: str, limit: int) -> ProviderDiscoveryResult:
        self.discovery_calls += 1
        index = self.discovery_calls
        candidates = (
            MetadataCandidate(
                external_work_id=f"OL-DISC-{index}",
                external_edition_id=None,
                title=f"Discovered Systems Book {index}",
                authors=(f"External Author {index}",),
                publication_year=2000 + index,
                identifiers={"isbn13": (f"978000000{index:04d}",)},
                cover_id=str(1000 + index),
            ),
        )
        return ProviderDiscoveryResult(
            request_key=f"query:{query}",
            endpoint="/search.json",
            status_code=200,
            raw_payload={"query": query, "limit": limit, "docs": [index]},
            candidates=candidates,
        )

    def fetch(self, candidate: MetadataCandidate) -> ProviderMetadataResult:
        self.fetch_calls += 1
        metadata = BookMetadata(
            external_work_id=candidate.external_work_id,
            external_edition_id=None,
            title=candidate.title,
            authors=candidate.authors,
            description=(
                "A behind the scenes technical narrative about organizational "
                "failure, human decisions, survival, and exploration."
            ),
            original_publication_year=candidate.publication_year,
            subjects=("Technology", "Organizational behavior"),
            cover_id=candidate.cover_id,
        )
        return ProviderMetadataResult(
            request_key=f"work:{candidate.external_work_id}",
            endpoint=f"/works/{candidate.external_work_id}.json",
            status_code=200,
            raw_payload={"key": f"/works/{candidate.external_work_id}"},
            metadata=metadata,
        )

    def search(self, lookup: BookLookup) -> ProviderSearchResult:
        raise AssertionError("Discovery must not repeat identity searches")


def test_discovery_is_bounded_provenanced_ranked_and_cached(
    db_session: Session,
) -> None:
    import_goodreads_csv(db_session, FIXTURES / "goodreads_sample.csv")
    sync_browse_facets(db_session)
    provider = FixtureDiscoveryProvider()

    first = discover_candidates(db_session, provider, limit=4)

    assert first.selected_count == 4
    assert first.enriched_count == 4
    assert first.external_request_count == len(CLUSTER_QUERIES) + 4
    assert provider.discovery_calls == len(CLUSTER_QUERIES)
    assert provider.fetch_calls == 4
    run = db_session.get(DiscoveryRun, first.run_id)
    assert run is not None and run.status == "completed"
    assert db_session.scalar(
        select(func.count())
        .select_from(DiscoveryQuery)
        .where(DiscoveryQuery.run_id == first.run_id)
    ) == len(CLUSTER_QUERIES)
    candidates = db_session.scalars(
        select(DiscoveryCandidate).where(DiscoveryCandidate.run_id == first.run_id)
    ).all()
    assert all(item.query_ids and item.cluster_slugs for item in candidates)
    assert all(item.enrichment_attempt_id is not None for item in candidates)
    assert all(
        db_session.scalar(
            select(LibraryEntry.id).where(LibraryEntry.work_id == item.work_id)
        )
        is None
        for item in candidates
    )
    assert all(
        db_session.scalar(select(Edition.id).where(Edition.work_id == item.work_id))
        is not None
        for item in candidates
    )

    recommendation = run_discovery_validation(db_session, first.run_id)
    assert recommendation.candidate_count == 4
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(RecommendationItem)
            .where(RecommendationItem.run_id == recommendation.run_id)
        )
        == 4
    )

    work_count = db_session.scalar(select(func.count()).select_from(Work))
    second = discover_candidates(db_session, provider, limit=4)
    second_recommendation = run_discovery_validation(db_session, second.run_id)

    assert second.run_id == first.run_id
    assert second.cache_hit is True
    assert second.external_request_count == 0
    assert provider.discovery_calls == len(CLUSTER_QUERIES)
    assert provider.fetch_calls == 4
    assert second_recommendation.run_id == recommendation.run_id
    assert second_recommendation.cached_run is True
    assert db_session.scalar(select(func.count()).select_from(Work)) == work_count

    item = db_session.scalar(
        select(RecommendationItem).where(
            RecommendationItem.run_id == recommendation.run_id
        )
    )
    assert item is not None
    explanation = db_session.scalar(
        select(RecommendationExplanation).where(
            RecommendationExplanation.recommendation_item_id == item.id
        )
    )
    alignment = db_session.scalar(
        select(RecommendationSignal).where(
            RecommendationSignal.recommendation_item_id == item.id,
            RecommendationSignal.signal_name == "discovery_alignment",
        )
    )
    assert explanation is not None
    assert explanation.structured_evidence["raw_rank"] >= 1
    assert explanation.structured_evidence["display_rank"] >= 1
    assert "quality_flags" in explanation.structured_evidence
    assert alignment is not None
    assert alignment.evidence_json["strength"] in {
        "strong",
        "moderate",
        "weak",
        "ambiguous",
        "unsupported",
    }
    first_feedback = record_recommendation_feedback(
        db_session, item.id, "add_to_want_to_read"
    )
    second_feedback = record_recommendation_feedback(
        db_session, item.id, "add_to_want_to_read"
    )
    assert first_feedback.id == second_feedback.id
    entry = db_session.scalar(
        select(LibraryEntry).where(LibraryEntry.work_id == item.work_id)
    )
    assert entry is not None and entry.status == "want_to_read"
