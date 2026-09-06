"""Typed values passed from web queries to templates."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class FacetItem:
    slug: str
    label: str
    category: str
    count: int | None = None


@dataclass(frozen=True)
class BookCard:
    id: int
    title: str
    author: str
    status: str
    rating: int | None
    year: int | None
    last_read_on: date | None
    cover_url: str | None
    description_excerpt: str | None
    shelves: tuple[str, ...] = ()
    facets: tuple[FacetItem, ...] = ()


@dataclass(frozen=True)
class FilterOption:
    value: str
    label: str
    count: int


@dataclass(frozen=True)
class FilterOptions:
    statuses: tuple[FilterOption, ...]
    shelves: tuple[FilterOption, ...]
    facets: tuple[FacetItem, ...]
    min_year: int | None
    max_year: int | None


@dataclass(frozen=True)
class LibraryFilters:
    q: str = ""
    status: str = ""
    shelf: str = ""
    year_from: int | None = None
    year_to: int | None = None
    rating: int | None = None
    concept: str = ""
    sort: str = "recent"
    page: int = 1
    per_page: int = 24
    view: str = "grid"


@dataclass(frozen=True)
class LibraryPage:
    books: tuple[BookCard, ...]
    filters: LibraryFilters
    options: FilterOptions
    total: int
    page: int
    total_pages: int


@dataclass(frozen=True)
class ReadingEventItem:
    started_on: date | None
    finished_on: date | None
    date_precision: str
    source: str


@dataclass(frozen=True)
class EditionItem:
    publisher: str | None
    publication_date: date | None
    publication_year: int | None
    page_count: int | None
    format: str | None
    language: str | None
    identifiers: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class BookDetail:
    id: int
    title: str
    subtitle: str | None
    authors: tuple[str, ...]
    description: str | None
    original_publication_year: int | None
    language: str | None
    fiction_status: str
    cover_url: str | None
    status: str
    rating: int | None
    notes: str | None
    first_read_on: date | None
    last_read_on: date | None
    shelves: tuple[str, ...]
    facets: tuple[FacetItem, ...]
    additional_subjects: tuple[str, ...]
    series: tuple[str, ...]
    editions: tuple[EditionItem, ...]
    reading_events: tuple[ReadingEventItem, ...]


@dataclass(frozen=True)
class ProvenanceClaimItem:
    field_name: str
    value: str
    provider: str
    source_kind: str
    status: str
    reason: str | None
    observed_at: datetime


@dataclass(frozen=True)
class ProvenanceView:
    work_id: int
    title: str
    claims: tuple[ProvenanceClaimItem, ...]
    accepted_matches: int
    provider_responses: int
    concept_claims: int
    latest_enrichment_at: datetime | None


@dataclass(frozen=True)
class RecommendationSignalItem:
    name: str
    raw_value: float
    weight: float
    contribution: float
    evidence: dict[str, Any]


@dataclass(frozen=True)
class RecommendationNeighborItem:
    title: str
    similarity: float


@dataclass(frozen=True)
class RecommendationCard:
    item_id: int
    work_id: int
    title: str
    author: str
    cover_url: str | None
    score: float
    match_label: str
    confidence_label: str
    repetitive: bool
    matching_traits: tuple[str, ...]
    signals: tuple[RecommendationSignalItem, ...]
    neighbors: tuple[RecommendationNeighborItem, ...]
    explanation: str
    feedback_actions: frozenset[str]
    in_library: bool
    discovery_clusters: tuple[str, ...]
    strongest_specific_evidence: tuple[str, ...]
    raw_rank: int
    diversified_rank: int
    eligible_rank: int | None
    display_eligible: bool
    eligibility_reasons: tuple[str, ...]
    eligibility_warnings: tuple[str, ...]
    eligibility_provenance: dict[str, Any]

    @property
    def id(self) -> int:
        return self.work_id


@dataclass(frozen=True)
class RecommendationSection:
    title: str
    description: str
    run_id: int | None
    cards: tuple[RecommendationCard, ...]
    kind: str


@dataclass(frozen=True)
class RecommendationCenter:
    recommended_for_you: RecommendationSection
    want_to_read: RecommendationSection
    withheld: tuple[RecommendationCard, ...]
    provider_message: str | None
