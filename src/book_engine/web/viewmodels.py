"""Typed values passed from web queries to templates."""

from dataclasses import dataclass
from datetime import date, datetime


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
