"""Provider-neutral enrichment contracts."""

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol


@dataclass(frozen=True)
class BookLookup:
    work_id: int
    edition_id: int
    title: str
    primary_author: str
    publication_year: int | None
    isbn10: str | None
    isbn13: str | None

    @property
    def request_key(self) -> str:
        if self.isbn13:
            return f"isbn13:{self.isbn13}"
        if self.isbn10:
            return f"isbn10:{self.isbn10}"
        return f"title-author:{self.title.casefold()}|{self.primary_author.casefold()}"


@dataclass(frozen=True)
class MetadataCandidate:
    external_work_id: str
    external_edition_id: str | None
    title: str
    authors: tuple[str, ...]
    publication_year: int | None
    identifiers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    matched_identifier: tuple[str, str] | None = None
    cover_id: str | None = None


@dataclass(frozen=True)
class CandidateEvaluation:
    candidate: MetadataCandidate
    isbn_match: bool
    title_similarity: float
    author_similarity: float
    year_difference: int | None
    score: float
    conflicts: tuple[str, ...] = ()
    work_signature: str = ""


@dataclass(frozen=True)
class MatchDecision:
    status: str
    method: str
    score: float | None
    candidate: MetadataCandidate | None
    evaluations: tuple[CandidateEvaluation, ...]
    reason: str
    equivalent_candidates: tuple[MetadataCandidate, ...] = ()


@dataclass(frozen=True)
class BookMetadata:
    external_work_id: str
    external_edition_id: str | None
    title: str
    subtitle: str | None = None
    authors: tuple[str, ...] = ()
    description: str | None = None
    original_publication_year: int | None = None
    publisher: str | None = None
    publication_date: date | None = None
    publication_year: int | None = None
    page_count: int | None = None
    language: str | None = None
    identifiers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    subjects: tuple[str, ...] = ()
    series: tuple[str, ...] = ()
    cover_id: str | None = None
    cover_urls: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderSearchResult:
    request_key: str
    endpoint: str
    status_code: int
    raw_payload: dict[str, Any]
    candidates: tuple[MetadataCandidate, ...]


@dataclass(frozen=True)
class ProviderMetadataResult:
    request_key: str
    endpoint: str
    status_code: int
    raw_payload: dict[str, Any]
    metadata: BookMetadata


class MetadataProvider(Protocol):
    name: str

    def search(self, lookup: BookLookup) -> ProviderSearchResult: ...

    def fetch(self, candidate: MetadataCandidate) -> ProviderMetadataResult: ...


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        request_key: str,
        endpoint: str,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.request_key = request_key
        self.endpoint = endpoint
        self.status_code = status_code
