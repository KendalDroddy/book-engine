"""Provider-neutral reputation contracts."""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ReputationLookup:
    work_id: int
    title: str
    author: str
    isbns: tuple[str, ...]


@dataclass(frozen=True)
class ReputationCandidate:
    provider_book_id: str
    title: str
    authors: tuple[str, ...]
    identifiers: tuple[str, ...]
    average_rating: float | None
    ratings_count: int | None


@dataclass(frozen=True)
class ReputationProviderResult:
    request_key: str
    endpoint: str
    status_code: int
    raw_response: dict[str, Any]
    candidates: tuple[ReputationCandidate, ...]
    request_count: int


class ReputationProvider(Protocol):
    name: str
    parser_version: str

    def lookup(self, lookup: ReputationLookup) -> ReputationProviderResult: ...


class ReputationProviderError(RuntimeError):
    pass
