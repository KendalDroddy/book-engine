import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from book_engine.catalog.models import Identifier, Work
from book_engine.importing.models import ImportRecord, ImportRun
from book_engine.library.models import LibraryEntry


def test_sqlite_enables_foreign_keys_and_wal(db_session: Session) -> None:
    assert db_session.scalar(text("PRAGMA foreign_keys")) == 1
    assert db_session.scalar(text("PRAGMA journal_mode")) == "wal"


def test_library_allows_only_one_entry_per_work(db_session: Session) -> None:
    work = Work(title="The Left Hand of Darkness")
    db_session.add(work)
    db_session.flush()
    db_session.add_all(
        [
            LibraryEntry(work_id=work.id, status="read"),
            LibraryEntry(work_id=work.id, status="want_to_read"),
        ]
    )

    with pytest.raises(IntegrityError):
        db_session.flush()


def test_identifier_must_have_exactly_one_owner(db_session: Session) -> None:
    db_session.add(Identifier(scheme="ISBN13", value="9780000000000", source="test"))

    with pytest.raises(IntegrityError):
        db_session.flush()


def test_import_record_preserves_raw_source_payload(db_session: Session) -> None:
    run = ImportRun(
        source_type="goodreads_csv",
        source_filename="goodreads.csv",
        file_checksum="a" * 64,
    )
    record = ImportRecord(
        import_run=run,
        source_row_number=2,
        source_record_key="123",
        raw_payload={"Book Id": "123", "Title": "A title", "My Rating": "0"},
        raw_payload_checksum="b" * 64,
    )
    db_session.add(record)
    db_session.commit()

    saved = db_session.get(ImportRecord, record.id)
    assert saved is not None
    assert saved.raw_payload["My Rating"] == "0"


def test_foreign_keys_are_enforced(db_session: Session) -> None:
    db_session.add(LibraryEntry(work_id=999_999, status="read"))

    with pytest.raises((IntegrityError, sqlite3.IntegrityError)):
        db_session.flush()
