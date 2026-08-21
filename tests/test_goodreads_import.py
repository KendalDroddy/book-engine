from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from book_engine.catalog.models import Author, Edition, Identifier, Work, WorkAuthor
from book_engine.enrichment.models import MetadataClaim
from book_engine.importing.goodreads import import_goodreads_csv, parse_goodreads_csv
from book_engine.importing.models import ImportRecord, ImportRun
from book_engine.library.models import LibraryEntry, ReadingEvent, Shelf

FIXTURE = Path(__file__).parent / "fixtures" / "goodreads_sample.csv"


def _count(session: Session, model: type[object]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def test_parser_supports_real_goodreads_cell_formats() -> None:
    rows = parse_goodreads_csv(FIXTURE)

    assert len(rows) == 3
    assert rows[0].isbn10 == "0123456789"
    assert rows[0].isbn13 == "9780123456786"
    assert rows[0].rating == 5
    assert rows[0].primary_author == "Primary Author"
    assert rows[0].additional_authors == ("Second Author", "Third Author")
    assert rows[0].date_read is not None
    assert rows[0].date_read.isoformat() == "2026-08-18"
    assert rows[1].isbn10 is None
    assert rows[1].isbn13 is None
    assert rows[1].rating is None
    assert rows[1].original_publication_year is None


def test_import_is_audited_and_idempotent(db_session: Session) -> None:
    first = import_goodreads_csv(db_session, FIXTURE)

    assert (first.created, first.updated, first.unchanged) == (3, 0, 0)
    assert (first.ambiguous, first.failed) == (0, 0)
    assert _count(db_session, Work) == 3
    assert _count(db_session, Edition) == 3
    assert _count(db_session, LibraryEntry) == 3
    assert _count(db_session, Identifier) == 7
    assert _count(db_session, Author) == 5
    assert _count(db_session, WorkAuthor) == 5
    assert _count(db_session, ReadingEvent) == 2
    assert _count(db_session, Shelf) == 2
    assert _count(db_session, ImportRun) == 1
    assert _count(db_session, ImportRecord) == 3
    assert _count(db_session, MetadataClaim) == 19

    reread = db_session.scalar(
        select(LibraryEntry).join(Work).where(Work.title == "A Reread Book")
    )
    assert reread is not None
    assert reread.first_read_on is None
    assert reread.last_read_on is not None
    assert reread.last_read_on.isoformat() == "2026-07-01"

    raw_record = db_session.scalar(
        select(ImportRecord).where(ImportRecord.source_record_key == "1001")
    )
    assert raw_record is not None
    assert raw_record.raw_payload["Author"] == "Primary  Author"
    assert raw_record.raw_payload["ISBN"] == '="0123456789"'

    second = import_goodreads_csv(db_session, FIXTURE)

    assert (second.created, second.updated, second.unchanged) == (0, 0, 3)
    assert (second.ambiguous, second.failed) == (0, 0)
    assert _count(db_session, Work) == 3
    assert _count(db_session, Edition) == 3
    assert _count(db_session, LibraryEntry) == 3
    assert _count(db_session, Identifier) == 7
    assert _count(db_session, Author) == 5
    assert _count(db_session, WorkAuthor) == 5
    assert _count(db_session, ReadingEvent) == 2
    assert _count(db_session, ImportRun) == 2
    assert _count(db_session, ImportRecord) == 6
    assert _count(db_session, MetadataClaim) == 19
