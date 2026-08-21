"""Model registry used by Alembic and tests."""

from book_engine.catalog.models import Author, Edition, Identifier, Work, WorkAuthor
from book_engine.importing.models import ImportRecord, ImportRun
from book_engine.library.models import (
    LibraryEntry,
    LibraryEntryShelf,
    ReadingEvent,
    Shelf,
)

__all__ = [
    "Author",
    "Edition",
    "Identifier",
    "ImportRecord",
    "ImportRun",
    "LibraryEntry",
    "LibraryEntryShelf",
    "ReadingEvent",
    "Shelf",
    "Work",
    "WorkAuthor",
]
