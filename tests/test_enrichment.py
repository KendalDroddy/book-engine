import json
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from book_engine.catalog.models import Edition, Identifier, Work
from book_engine.enrichment.matching import decide_match
from book_engine.enrichment.models import (
    Concept,
    CoverCandidate,
    EnrichmentAttempt,
    EnrichmentRun,
    MetadataClaim,
    MetadataMatch,
    ProviderResponse,
    WorkConceptClaim,
)
from book_engine.enrichment.providers.openlibrary import (
    parse_metadata_payload,
    parse_search_payload,
)
from book_engine.enrichment.service import enrich_work
from book_engine.enrichment.types import (
    BookLookup,
    MetadataCandidate,
    ProviderError,
    ProviderMetadataResult,
    ProviderSearchResult,
)
from book_engine.importing.goodreads import import_goodreads_csv

FIXTURES = Path(__file__).parent / "fixtures"


def _json_fixture(name: str) -> dict[str, object]:
    loaded: object = json.loads((FIXTURES / "openlibrary" / name).read_text())
    assert isinstance(loaded, dict)
    return loaded


class FakeProvider:
    name = "openlibrary"

    def __init__(
        self, *, candidates: tuple[MetadataCandidate, ...] | None = None
    ) -> None:
        lookup = BookLookup(
            work_id=1,
            edition_id=1,
            title="A Read Book",
            primary_author="Primary Author",
            publication_year=2020,
            isbn10="0123456789",
            isbn13="9780123456786",
        )
        self.search_payload = _json_fixture("search_isbn.json")
        self.work_payload = _json_fixture("work.json")
        self.edition_payload = _json_fixture("edition.json")
        self.candidates = (
            candidates
            if candidates is not None
            else parse_search_payload(self.search_payload, lookup)
        )
        self.search_calls = 0
        self.fetch_calls = 0

    def search(self, lookup: BookLookup) -> ProviderSearchResult:
        self.search_calls += 1
        return ProviderSearchResult(
            request_key=lookup.request_key,
            endpoint="/search.json",
            status_code=200,
            raw_payload=self.search_payload,
            candidates=self.candidates,
        )

    def fetch(self, candidate: MetadataCandidate) -> ProviderMetadataResult:
        self.fetch_calls += 1
        return ProviderMetadataResult(
            request_key=f"work:{candidate.external_work_id}",
            endpoint=f"/works/{candidate.external_work_id}.json",
            status_code=200,
            raw_payload={"work": self.work_payload, "edition": self.edition_payload},
            metadata=parse_metadata_payload(
                candidate, self.work_payload, self.edition_payload
            ),
        )


class FailingProvider(FakeProvider):
    def search(self, lookup: BookLookup) -> ProviderSearchResult:
        raise ProviderError(
            "temporary outage",
            request_key=lookup.request_key,
            endpoint="/search.json",
            status_code=503,
        )


def _count(session: Session, model: type[object]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def _import_fixture(session: Session) -> None:
    import_goodreads_csv(session, FIXTURES / "goodreads_sample.csv")


def test_enrichment_is_provenanced_and_idempotent(db_session: Session) -> None:
    _import_fixture(db_session)
    provider = FakeProvider()

    first = enrich_work(db_session, 1, provider)

    assert first.status == "succeeded"
    assert first.decision is not None
    assert first.decision.status == "accepted"
    assert first.decision.method == "exact_isbn"
    assert "description" in first.accepted_fields
    assert "publisher" in first.candidate_fields
    assert provider.search_calls == 1
    assert provider.fetch_calls == 1

    work = db_session.get(Work, 1)
    edition = db_session.get(Edition, 1)
    assert work is not None and edition is not None
    assert work.description == "A recorded description."
    assert edition.publisher == "Example Press"
    assert edition.cover_source == "openlibrary"
    assert _count(db_session, ProviderResponse) == 2
    assert _count(db_session, MetadataMatch) == 1
    assert _count(db_session, Concept) == 2
    assert _count(db_session, WorkConceptClaim) == 2
    assert _count(db_session, CoverCandidate) == 1

    description_claim = db_session.scalar(
        select(MetadataClaim).where(
            MetadataClaim.work_id == 1,
            MetadataClaim.field_name == "description",
        )
    )
    assert description_claim is not None
    assert description_claim.source_kind == "provider"
    assert description_claim.provider_response_id is not None
    assert description_claim.status == "accepted"

    title_claims = db_session.scalars(
        select(MetadataClaim).where(
            MetadataClaim.work_id == 1, MetadataClaim.field_name == "title"
        )
    ).all()
    assert {claim.source_kind for claim in title_claims} == {
        "goodreads_import",
        "provider",
    }
    assert {claim.status for claim in title_claims} == {"accepted", "candidate"}

    goodreads_identifier = db_session.scalar(
        select(Identifier).where(
            Identifier.edition_id == 1,
            Identifier.scheme == "goodreads_book_id",
        )
    )
    assert goodreads_identifier is not None
    assert goodreads_identifier.import_record_id is not None

    second = enrich_work(db_session, 1, provider)
    assert second.status == "skipped_cached"
    assert provider.search_calls == 1
    assert provider.fetch_calls == 1
    assert _count(db_session, EnrichmentRun) == 2
    assert _count(db_session, EnrichmentAttempt) == 2
    assert _count(db_session, ProviderResponse) == 2
    assert _count(db_session, CoverCandidate) == 1


def test_duplicate_provider_works_are_one_work_match(db_session: Session) -> None:
    _import_fixture(db_session)
    base = FakeProvider().candidates[0]
    other = MetadataCandidate(
        external_work_id="OL9999W",
        external_edition_id=None,
        title=base.title,
        authors=base.authors,
        publication_year=base.publication_year,
        identifiers=base.identifiers,
        matched_identifier=base.matched_identifier,
    )
    provider = FakeProvider(candidates=(base, other))

    report = enrich_work(db_session, 1, provider)

    assert report.status == "succeeded"
    assert report.decision is not None
    assert report.decision.candidate == base
    assert report.decision.equivalent_candidates == (other,)
    assert provider.fetch_calls == 1
    work = db_session.get(Work, 1)
    assert work is not None
    assert work.description == "A recorded description."
    assert _count(db_session, MetadataMatch) == 2
    equivalent_match = db_session.scalar(
        select(MetadataMatch).where(MetadataMatch.external_work_id == "OL9999W")
    )
    assert equivalent_match is not None
    assert equivalent_match.evidence["relation"] == "equivalent"


def test_only_selected_candidate_is_recorded_as_accepted(db_session: Session) -> None:
    _import_fixture(db_session)
    selected = FakeProvider().candidates[0]
    rejected = MetadataCandidate(
        external_work_id="OL-WEAKER",
        external_edition_id=None,
        title="A Read Book",
        authors=("Primary Author",),
        publication_year=2010,
    )
    provider = FakeProvider(candidates=(selected, rejected))

    report = enrich_work(db_session, 1, provider)

    assert report.status == "succeeded"
    matches = db_session.execute(
        select(MetadataMatch.external_work_id, MetadataMatch.status).order_by(
            MetadataMatch.external_work_id
        )
    ).all()
    assert [tuple(row) for row in matches] == [
        ("OL-WEAKER", "rejected"),
        ("OL1000W", "accepted"),
    ]


def test_provider_failure_is_audited_and_retryable(db_session: Session) -> None:
    _import_fixture(db_session)

    report = enrich_work(db_session, 1, FailingProvider())

    assert report.status == "failed"
    assert report.error == "temporary outage"
    attempt = db_session.get(EnrichmentAttempt, report.attempt_id)
    assert attempt is not None
    assert attempt.status == "failed"
    assert attempt.next_retry_at is not None
    response = db_session.scalar(select(ProviderResponse))
    assert response is not None
    assert response.outcome == "failed"
    assert response.status_code == 503


def test_provider_miss_is_audited_without_fetch(db_session: Session) -> None:
    _import_fixture(db_session)
    provider = FakeProvider(candidates=())

    report = enrich_work(db_session, 1, provider)

    assert report.status == "missed"
    assert report.decision is not None
    assert report.decision.reason == "Provider returned no candidates"
    assert provider.search_calls == 1
    assert provider.fetch_calls == 0
    assert _count(db_session, MetadataMatch) == 0


def test_exact_isbn_with_conflicting_identity_is_not_accepted() -> None:
    lookup = BookLookup(
        work_id=1,
        edition_id=1,
        title="A Read Book",
        primary_author="Primary Author",
        publication_year=2020,
        isbn10=None,
        isbn13="9780123456786",
    )
    conflict = MetadataCandidate(
        external_work_id="OL-CONFLICT",
        external_edition_id=None,
        title="A Completely Different Book",
        authors=("Unrelated Writer",),
        publication_year=2020,
        identifiers={"isbn13": ("9780123456786",)},
        matched_identifier=("isbn13", "9780123456786"),
    )

    decision = decide_match(lookup, (conflict,))

    assert decision.status == "missed"
    assert decision.candidate is None
    assert "ISBN matched but primary authors were materially different" in (
        decision.evaluations[0].conflicts
    )


def test_series_suffix_does_not_block_conservative_title_match() -> None:
    lookup = BookLookup(
        work_id=1,
        edition_id=1,
        title="A Story (Example Saga, #2)",
        primary_author="Known Author",
        publication_year=2020,
        isbn10=None,
        isbn13=None,
    )
    candidate = MetadataCandidate(
        external_work_id="OL-SERIES",
        external_edition_id=None,
        title="A Story",
        authors=("Known Author",),
        publication_year=2019,
    )

    decision = decide_match(lookup, (candidate,))

    assert decision.status == "accepted"
    assert decision.method == "work_signature"


def test_missing_candidate_year_does_not_block_strong_work_identity() -> None:
    lookup = BookLookup(
        work_id=1,
        edition_id=1,
        title="A Story",
        primary_author="Known Author",
        publication_year=2020,
        isbn10=None,
        isbn13=None,
    )
    candidate = MetadataCandidate(
        external_work_id="OL-NO-YEAR",
        external_edition_id=None,
        title="A Story",
        authors=("Known Author",),
        publication_year=None,
    )

    decision = decide_match(lookup, (candidate,))

    assert decision.status == "accepted"
    assert decision.candidate == candidate


def test_shortened_subtitle_is_safe_with_exact_isbn_and_author() -> None:
    lookup = BookLookup(
        work_id=1,
        edition_id=1,
        title="The Boys in the Light: A Story of Survival, Faith, and Brotherhood",
        primary_author="Nina Willner",
        publication_year=None,
        isbn10=None,
        isbn13="9780593471272",
    )
    candidate = MetadataCandidate(
        external_work_id="OL-BOYS",
        external_edition_id=None,
        title="Boys in the Light",
        authors=("Nina Willner",),
        publication_year=2025,
        identifiers={"isbn13": ("9780593471272",)},
        matched_identifier=("isbn13", "9780593471272"),
    )

    decision = decide_match(lookup, (candidate,))

    assert decision.status == "accepted"
    assert decision.method == "exact_isbn"
    assert decision.evaluations[0].title_similarity == 1.0


def test_minor_year_difference_and_sparse_duplicate_do_not_block_work_match() -> None:
    lookup = BookLookup(
        work_id=1,
        edition_id=1,
        title="The Return of the King (The Lord of the Rings, #3)",
        primary_author="J.R.R. Tolkien",
        publication_year=1955,
        isbn10=None,
        isbn13=None,
    )
    rich_candidate = MetadataCandidate(
        external_work_id="OL-RICH",
        external_edition_id=None,
        title="The Return of the King",
        authors=("J.R.R. Tolkien",),
        publication_year=1950,
        identifiers={"isbn13": ("9780000000001", "9780000000002")},
        cover_id="123",
    )
    sparse_duplicate = MetadataCandidate(
        external_work_id="OL-SPARSE",
        external_edition_id=None,
        title="The Return of the King",
        authors=("J.R.R. Tolkien",),
        publication_year=None,
    )

    decision = decide_match(lookup, (sparse_duplicate, rich_candidate))

    assert decision.status == "accepted"
    assert decision.candidate == rich_candidate
    assert decision.equivalent_candidates == (sparse_duplicate,)


def test_distinct_plausible_work_signatures_remain_ambiguous() -> None:
    lookup = BookLookup(
        work_id=1,
        edition_id=1,
        title="A Great Story",
        primary_author="Known Author",
        publication_year=2020,
        isbn10=None,
        isbn13=None,
    )
    candidates = (
        MetadataCandidate(
            external_work_id="OL-STORY",
            external_edition_id=None,
            title="A Great Story",
            authors=("Known Author",),
            publication_year=2020,
        ),
        MetadataCandidate(
            external_work_id="OL-STORIES",
            external_edition_id=None,
            title="A Great Story 2",
            authors=("Known Author",),
            publication_year=2020,
        ),
    )

    decision = decide_match(lookup, candidates)

    assert decision.status == "ambiguous"
    assert decision.candidate is None


def test_materially_different_publication_variants_are_rejected() -> None:
    lookup = BookLookup(
        work_id=1,
        edition_id=1,
        title="The Return of the King",
        primary_author="J.R.R. Tolkien",
        publication_year=1955,
        isbn10=None,
        isbn13=None,
    )
    variants = (
        MetadataCandidate(
            external_work_id="OL-SPLIT",
            external_edition_id=None,
            title="The Return of the King [2/2]",
            authors=("J.R.R. Tolkien",),
            publication_year=1989,
        ),
        MetadataCandidate(
            external_work_id="OL-SET",
            external_edition_id=None,
            title="The Lord of the Rings 3 Book Set",
            authors=("J.R.R. Tolkien",),
            publication_year=2020,
        ),
        MetadataCandidate(
            external_work_id="OL-ADAPTATION",
            external_edition_id=None,
            title="The Return of the King Graphic Adaptation",
            authors=("J.R.R. Tolkien",),
            publication_year=2020,
        ),
    )

    decision = decide_match(lookup, variants)

    assert decision.status == "missed"
    assert all(evaluation.conflicts for evaluation in decision.evaluations)
