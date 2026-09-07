from pathlib import Path

import pytest
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
from book_engine.reputation.models import ReputationFetch, ReputationObservation
from book_engine.reputation.service import enrich_discovery_reputation
from book_engine.reputation.types import (
    ReputationCandidate,
    ReputationLookup,
    ReputationProviderResult,
)
from book_engine.web.facets import sync_browse_facets
from book_engine.web.recommendations import (
    get_recommendation_center,
    record_recommendation_feedback,
)

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


class FixtureReputationProvider:
    name = "fixture_reputation"
    parser_version = "fixture-v1"

    def __init__(self) -> None:
        self.calls = 0

    def lookup(self, lookup: ReputationLookup) -> ReputationProviderResult:
        self.calls += 1
        return ReputationProviderResult(
            request_key=f"work:{lookup.work_id}",
            endpoint="fixture",
            status_code=200,
            raw_response={"fixture": lookup.work_id},
            candidates=(
                ReputationCandidate(
                    provider_book_id=f"fixture-{lookup.work_id}",
                    title=lookup.title,
                    authors=(lookup.author,),
                    identifiers=lookup.isbns,
                    average_rating=4.2,
                    ratings_count=5000,
                ),
            ),
            request_count=1,
        )


class FailingReputationProvider:
    name = "failing_reputation"
    parser_version = "fixture-v1"

    def __init__(self) -> None:
        self.calls = 0

    def lookup(self, lookup: ReputationLookup) -> ReputationProviderResult:
        self.calls += 1
        raise RuntimeError("fixture provider unavailable")


def test_forced_reputation_refresh_preserves_success_and_failure_history(
    db_session: Session,
) -> None:
    import_goodreads_csv(db_session, FIXTURES / "goodreads_sample.csv")
    discovery = discover_candidates(db_session, FixtureDiscoveryProvider(), limit=1)
    recommendation = run_discovery_validation(db_session, discovery.run_id)
    provider = FixtureReputationProvider()

    def refresh(*, force: bool = False) -> object:
        return enrich_discovery_reputation(
            db_session, discovery.run_id, provider, force_refresh=force
        )

    refresh()
    original_fetch = db_session.scalars(select(ReputationFetch)).one()
    original_observation = db_session.scalars(select(ReputationObservation)).one()
    original_payload = dict(original_fetch.raw_response or {})
    original_time = original_fetch.fetched_at
    refresh()
    assert provider.calls == 1
    refresh(force=True)
    assert provider.calls == 2
    assert (
        db_session.scalar(select(func.count()).select_from(ReputationObservation)) == 2
    )

    failing = FailingReputationProvider()
    failing.name = provider.name
    failed = enrich_discovery_reputation(
        db_session, discovery.run_id, failing, force_refresh=True
    )
    assert failed.failed == 1
    failure = db_session.scalar(
        select(ReputationFetch).where(ReputationFetch.status == "failed")
    )
    assert failure is not None
    failure_time = failure.fetched_at
    cooldown = enrich_discovery_reputation(db_session, discovery.run_id, provider)
    assert cooldown.cache_hits == 1
    assert cooldown.external_requests == 0
    recovered = enrich_discovery_reputation(
        db_session, discovery.run_id, provider, force_refresh=True
    )
    assert recovered.succeeded == 1
    assert recovered.cache_hits == 0
    assert recovered.external_requests == 1
    assert provider.calls == 3
    assert db_session.scalar(select(func.count()).select_from(ReputationFetch)) == 4
    assert (
        db_session.scalar(select(func.count()).select_from(ReputationObservation)) == 3
    )
    db_session.expire_all()
    assert original_fetch.raw_response == original_payload
    assert original_fetch.fetched_at == original_time
    assert original_fetch.status == "succeeded"
    assert original_observation.average_rating == 4.2
    assert failure.error_message == "fixture provider unavailable"
    assert failure.fetched_at == failure_time
    assert failure.status == "failed"
    latest = db_session.scalar(
        select(ReputationFetch).order_by(ReputationFetch.id.desc())
    )
    assert latest is not None
    assert latest.decision_provenance["previous_fetch_id"] == failure.id
    assert latest.decision_provenance["force_refresh"] is True
    item = db_session.scalar(
        select(RecommendationItem).where(
            RecommendationItem.run_id == recommendation.run_id
        )
    )
    assert item is not None and item.reputation_observation_id is not None
    assert (
        enrich_discovery_reputation(db_session, discovery.run_id, provider).cache_hits
        == 1
    )
    assert provider.calls == 3


@pytest.mark.parametrize("status", ["missed", "ambiguous"])
def test_force_refresh_bypasses_negative_result_cache(
    db_session: Session, status: str
) -> None:
    import_goodreads_csv(db_session, FIXTURES / "goodreads_sample.csv")
    discovery = discover_candidates(db_session, FixtureDiscoveryProvider(), limit=1)
    run_discovery_validation(db_session, discovery.run_id)
    provider = FixtureReputationProvider()
    enrich_discovery_reputation(db_session, discovery.run_id, provider)
    cached = db_session.scalars(select(ReputationFetch)).one()
    cached.status = status
    db_session.commit()
    assert (
        enrich_discovery_reputation(db_session, discovery.run_id, provider).cache_hits
        == 1
    )
    forced = enrich_discovery_reputation(
        db_session, discovery.run_id, provider, force_refresh=True
    )
    assert forced.external_requests == 1
    assert forced.cache_hits == 0
    assert cached.status == status
    assert provider.calls == 2


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

    reputation_provider = FixtureReputationProvider()
    reputation = enrich_discovery_reputation(
        db_session, first.run_id, reputation_provider
    )
    cached_reputation = enrich_discovery_reputation(
        db_session, first.run_id, reputation_provider
    )
    assert reputation.succeeded == 4
    assert reputation.external_requests == 4
    assert cached_reputation.cache_hits == 4
    assert cached_reputation.external_requests == 0
    assert reputation_provider.calls == 4
    assert (
        db_session.scalar(select(func.count()).select_from(ReputationObservation)) == 4
    )
    failing_provider = FailingReputationProvider()
    failed = enrich_discovery_reputation(db_session, first.run_id, failing_provider)
    cached_failed = enrich_discovery_reputation(
        db_session, first.run_id, failing_provider
    )
    assert failed.failed == 4
    assert cached_failed.failed == 4
    assert cached_failed.cache_hits == 4
    assert failing_provider.calls == 4
    assert (
        db_session.scalar(select(func.count()).select_from(ReputationObservation)) == 4
    )

    center = get_recommendation_center(db_session)
    assert center.recommended_for_you.run_id == recommendation.run_id
    assert all(card.display_eligible for card in center.recommended_for_you.cards)
    assert all(not card.in_library for card in center.recommended_for_you.cards)
    assert center.want_to_read.title == "Already on Your Radar"

    withheld_item = db_session.scalar(
        select(RecommendationItem)
        .where(RecommendationItem.run_id == recommendation.run_id)
        .order_by(RecommendationItem.rank.desc())
    )
    assert withheld_item is not None
    withheld_item.display_eligible = False
    withheld_item.eligible_rank = None
    withheld_item.eligibility_reasons = ["fixture_diagnostic"]
    db_session.commit()
    center = get_recommendation_center(db_session)
    assert all(
        card.item_id != withheld_item.id for card in center.recommended_for_you.cards
    )
    assert withheld_item.id in {card.item_id for card in center.withheld}

    item = db_session.scalar(
        select(RecommendationItem).where(
            RecommendationItem.run_id == recommendation.run_id,
            RecommendationItem.display_eligible.is_(True),
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
    center = get_recommendation_center(db_session)
    assert item.work_id not in {
        card.work_id for card in center.recommended_for_you.cards
    }
