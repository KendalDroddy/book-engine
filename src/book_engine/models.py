"""Model registry used by Alembic and tests."""

from book_engine.catalog.models import Author, Edition, Identifier, Work, WorkAuthor
from book_engine.enrichment.models import (
    Concept,
    CoverCandidate,
    EnrichmentAttempt,
    EnrichmentRun,
    MetadataClaim,
    MetadataMatch,
    ProviderResponse,
    Series,
    WorkConceptClaim,
    WorkSeriesClaim,
)
from book_engine.importing.models import ImportRecord, ImportRun
from book_engine.library.models import (
    LibraryEntry,
    LibraryEntryShelf,
    ReadingEvent,
    Shelf,
)

__all__ = [
    "Author",
    "Concept",
    "CoverCandidate",
    "Edition",
    "EnrichmentAttempt",
    "EnrichmentRun",
    "Identifier",
    "ImportRecord",
    "ImportRun",
    "LibraryEntry",
    "LibraryEntryShelf",
    "MetadataClaim",
    "MetadataMatch",
    "ProviderResponse",
    "ReadingEvent",
    "Shelf",
    "Series",
    "Work",
    "WorkAuthor",
    "WorkConceptClaim",
    "WorkSeriesClaim",
]
