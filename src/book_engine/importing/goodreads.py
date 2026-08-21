"""Goodreads CSV parsing, reconciliation, and import auditing."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from book_engine.catalog.models import Author, Edition, Identifier, Work, WorkAuthor
from book_engine.enrichment.models import MetadataClaim
from book_engine.importing.models import ImportRecord, ImportRun
from book_engine.library.models import (
    LibraryEntry,
    LibraryEntryShelf,
    ReadingEvent,
    Shelf,
)

GOODREADS_HEADERS = (
    "Book Id",
    "Title",
    "Author",
    "Author l-f",
    "Additional Authors",
    "ISBN",
    "ISBN13",
    "My Rating",
    "Publisher",
    "Binding",
    "Number of Pages",
    "Year Published",
    "Original Publication Year",
    "Date Read",
    "Date Added",
    "Bookshelves",
    "Bookshelves with positions",
    "Exclusive Shelf",
    "My Review",
    "Spoiler",
    "Private Notes",
    "Read Count",
    "Owned Copies",
)

STATUS_MAP = {
    "read": "read",
    "currently-reading": "reading",
    "to-read": "want_to_read",
}


class GoodreadsFormatError(ValueError):
    """Raised when a CSV cannot be interpreted as a Goodreads library export."""


@dataclass(frozen=True)
class GoodreadsRow:
    row_number: int
    raw: dict[str, str]
    book_id: str
    title: str
    primary_author: str
    primary_author_sort: str
    additional_authors: tuple[str, ...]
    isbn10: str | None
    isbn13: str | None
    rating: int | None
    publisher: str | None
    binding: str | None
    page_count: int | None
    publication_year: int | None
    original_publication_year: int | None
    date_read: date | None
    date_added: date
    shelves: tuple[str, ...]
    exclusive_shelf: str
    read_count: int


@dataclass(frozen=True)
class ImportReport:
    import_run_id: int
    total: int
    created: int
    updated: int
    unchanged: int
    ambiguous: int
    failed: int


@dataclass(frozen=True)
class Resolution:
    status: str
    work: Work | None = None
    edition: Edition | None = None
    notes: str | None = None


def parse_goodreads_csv(path: Path) -> list[GoodreadsRow]:
    """Parse a Goodreads export without modifying or normalizing its raw fields."""
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        actual_headers = tuple(reader.fieldnames or ())
        if actual_headers != GOODREADS_HEADERS:
            raise GoodreadsFormatError(
                "Unexpected Goodreads headers. "
                f"Expected {list(GOODREADS_HEADERS)!r}; got {list(actual_headers)!r}"
            )

        rows: list[GoodreadsRow] = []
        for row_number, raw_row in enumerate(reader, start=2):
            if None in raw_row:
                raise GoodreadsFormatError(f"Row {row_number} has too many columns")
            raw = {header: raw_row[header] for header in GOODREADS_HEADERS}
            rows.append(_parse_row(row_number, raw))
        return rows


def import_goodreads_csv(
    session: Session, path: Path, *, source_filename: str | None = None
) -> ImportReport:
    """Import a Goodreads export and retain an audit record for every source row."""
    rows = parse_goodreads_csv(path)
    file_checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    run = ImportRun(
        source_type="goodreads_csv",
        source_filename=source_filename or path.name,
        file_checksum=file_checksum,
        started_at=_utc_now(),
        status="running",
        total_rows=len(rows),
    )
    session.add(run)
    session.flush()

    counts = {
        key: 0 for key in ("created", "updated", "unchanged", "ambiguous", "failed")
    }

    for row in rows:
        try:
            with session.begin_nested():
                resolution = _reconcile_row(session, row)
        except Exception as exc:  # Keep later source rows importable and audited.
            resolution = Resolution(
                status="failed", notes=f"{type(exc).__name__}: {exc}"
            )

        counts[resolution.status] += 1
        import_record = ImportRecord(
            import_run_id=run.id,
            source_row_number=row.row_number,
            source_record_key=row.book_id,
            raw_payload=row.raw,
            raw_payload_checksum=_payload_checksum(row.raw),
            work_id=resolution.work.id if resolution.work is not None else None,
            edition_id=(
                resolution.edition.id if resolution.edition is not None else None
            ),
            resolution_status=resolution.status,
            resolution_notes=resolution.notes,
        )
        session.add(import_record)
        session.flush()
        _record_goodreads_provenance(session, import_record, resolution)

    run.created_rows = counts["created"]
    run.updated_rows = counts["updated"]
    run.unchanged_rows = counts["unchanged"]
    run.ambiguous_rows = counts["ambiguous"]
    run.failed_rows = counts["failed"]
    run.completed_at = _utc_now()
    run.status = "completed_with_errors" if counts["failed"] else "completed"
    session.commit()

    return ImportReport(
        import_run_id=run.id,
        total=len(rows),
        created=counts["created"],
        updated=counts["updated"],
        unchanged=counts["unchanged"],
        ambiguous=counts["ambiguous"],
        failed=counts["failed"],
    )


def _parse_row(row_number: int, raw: dict[str, str]) -> GoodreadsRow:
    book_id = _required(raw["Book Id"], "Book Id", row_number)
    title = _required(raw["Title"], "Title", row_number)
    author = _required(raw["Author"], "Author", row_number)
    exclusive_shelf = _required(raw["Exclusive Shelf"], "Exclusive Shelf", row_number)
    if exclusive_shelf not in STATUS_MAP:
        raise GoodreadsFormatError(
            f"Row {row_number} has unsupported Exclusive Shelf {exclusive_shelf!r}"
        )

    shelves = _split_list(raw["Bookshelves"])
    if exclusive_shelf not in shelves:
        shelves = (*shelves, exclusive_shelf)

    rating_decimal = _decimal(raw["My Rating"], "My Rating", row_number)
    if rating_decimal is not None and rating_decimal != rating_decimal.to_integral():
        raise GoodreadsFormatError(f"Row {row_number} has a non-integral rating")
    rating = int(rating_decimal) if rating_decimal else None
    if rating is not None and not 1 <= rating <= 5:
        raise GoodreadsFormatError(f"Row {row_number} has a rating outside 0-5")

    return GoodreadsRow(
        row_number=row_number,
        raw=raw,
        book_id=book_id,
        title=_clean_text(title),
        primary_author=_clean_text(author),
        primary_author_sort=_clean_text(raw["Author l-f"]),
        additional_authors=tuple(
            _clean_text(name) for name in _split_list(raw["Additional Authors"])
        ),
        isbn10=_isbn(raw["ISBN"], 10, row_number),
        isbn13=_isbn(raw["ISBN13"], 13, row_number),
        rating=rating,
        publisher=_optional_clean_text(raw["Publisher"]),
        binding=_optional_clean_text(raw["Binding"]),
        page_count=_integer(raw["Number of Pages"], "Number of Pages", row_number),
        publication_year=_integer(raw["Year Published"], "Year Published", row_number),
        original_publication_year=_integer(
            raw["Original Publication Year"],
            "Original Publication Year",
            row_number,
        ),
        date_read=_date(raw["Date Read"], "Date Read", row_number),
        date_added=_required_date(raw["Date Added"], "Date Added", row_number),
        shelves=shelves,
        exclusive_shelf=exclusive_shelf,
        read_count=_integer(raw["Read Count"], "Read Count", row_number) or 0,
    )


def _reconcile_row(session: Session, row: GoodreadsRow) -> Resolution:
    identifier_matches = _identifier_matches(session, row)
    if len(identifier_matches) > 1:
        return Resolution(
            status="ambiguous",
            notes="Identifiers resolve to multiple canonical works",
        )

    edition: Edition | None = None
    created = False
    if identifier_matches:
        work, edition = next(iter(identifier_matches.values()))
    else:
        title_matches = _title_author_matches(session, row)
        if len(title_matches) > 1:
            return Resolution(
                status="ambiguous",
                notes="Title and primary author match multiple canonical works",
            )
        if title_matches:
            work = title_matches[0]
        else:
            work = Work(title=row.title)
            session.add(work)
            session.flush()
            created = True
    changed = False

    changed |= _set(work, "title", row.title)
    changed |= _set(work, "original_publication_year", row.original_publication_year)

    if edition is None:
        edition = Edition(work=work)
        session.add(edition)
        session.flush()
        changed = True

    changed |= _set(edition, "title", row.title)
    changed |= _set(edition, "publisher", row.publisher)
    changed |= _set(edition, "format", row.binding)
    changed |= _set(edition, "page_count", row.page_count)
    changed |= _set(edition, "publication_year", row.publication_year)

    changed |= _ensure_identifier(session, edition, "goodreads_book_id", row.book_id)
    if row.isbn10:
        changed |= _ensure_identifier(session, edition, "isbn10", row.isbn10)
    if row.isbn13:
        changed |= _ensure_identifier(session, edition, "isbn13", row.isbn13)

    changed |= _sync_authors(session, work, row)
    changed |= _sync_library_entry(session, work, row)
    session.flush()

    if created:
        return Resolution(status="created", work=work, edition=edition)
    return Resolution(
        status="updated" if changed else "unchanged",
        work=work,
        edition=edition,
    )


def _identifier_matches(
    session: Session, row: GoodreadsRow
) -> dict[int, tuple[Work, Edition | None]]:
    candidates = [("goodreads_book_id", row.book_id)]
    if row.isbn10:
        candidates.append(("isbn10", row.isbn10))
    if row.isbn13:
        candidates.append(("isbn13", row.isbn13))

    matches: dict[int, tuple[Work, Edition | None]] = {}
    for scheme, value in candidates:
        identifiers = session.scalars(
            select(Identifier).where(
                Identifier.scheme == scheme, Identifier.value == value
            )
        ).all()
        for identifier in identifiers:
            if identifier.edition is not None:
                matches[identifier.edition.work.id] = (
                    identifier.edition.work,
                    identifier.edition,
                )
            elif identifier.work is not None:
                matches[identifier.work.id] = (identifier.work, None)
    return matches


def _title_author_matches(session: Session, row: GoodreadsRow) -> list[Work]:
    works = session.scalars(select(Work).where(Work.title == row.title)).unique().all()
    expected_author = _match_key(row.primary_author)
    return [
        work
        for work in works
        if any(
            link.position == 0 and _match_key(link.author.name) == expected_author
            for link in work.author_links
        )
    ]


def _sync_authors(session: Session, work: Work, row: GoodreadsRow) -> bool:
    changed = False
    expected = ((row.primary_author, row.primary_author_sort),) + tuple(
        (name, "") for name in row.additional_authors
    )
    existing = {
        (link.position, _match_key(link.author.name)) for link in work.author_links
    }

    for position, (name, sort_name) in enumerate(expected):
        if (position, _match_key(name)) in existing:
            continue
        author = _find_or_create_author(session, name, sort_name or None)
        session.add(
            WorkAuthor(
                work=work,
                author=author,
                role="author",
                position=position,
            )
        )
        changed = True
    return changed


def _find_or_create_author(
    session: Session, name: str, sort_name: str | None
) -> Author:
    match_key = _match_key(name)
    for author in session.scalars(select(Author)).all():
        if _match_key(author.name) == match_key:
            if sort_name and not author.sort_name:
                author.sort_name = sort_name
            return author
    author = Author(name=name, sort_name=sort_name)
    session.add(author)
    session.flush()
    return author


def _sync_library_entry(session: Session, work: Work, row: GoodreadsRow) -> bool:
    entry = session.scalar(select(LibraryEntry).where(LibraryEntry.work_id == work.id))
    changed = False
    if entry is None:
        entry = LibraryEntry(
            work_id=work.id,
            status=STATUS_MAP[row.exclusive_shelf],
            personal_rating=row.rating,
        )
        session.add(entry)
        session.flush()
        changed = True

    changed |= _set(entry, "status", STATUS_MAP[row.exclusive_shelf])
    changed |= _set(entry, "personal_rating", row.rating)
    if row.date_read:
        if row.read_count == 1:
            changed |= _set(entry, "first_read_on", row.date_read)
        changed |= _set(entry, "last_read_on", row.date_read)
        event = session.scalar(
            select(ReadingEvent).where(
                ReadingEvent.library_entry_id == entry.id,
                ReadingEvent.started_on.is_(None),
                ReadingEvent.finished_on == row.date_read,
                ReadingEvent.source == "goodreads_csv",
            )
        )
        if event is None:
            session.add(
                ReadingEvent(
                    library_entry=entry,
                    finished_on=row.date_read,
                    date_precision="day",
                    source="goodreads_csv",
                )
            )
            changed = True

    existing_shelves = {link.shelf.name for link in entry.shelf_links}
    for shelf_name in row.shelves:
        if shelf_name in existing_shelves:
            continue
        shelf = session.scalar(select(Shelf).where(Shelf.name == shelf_name))
        if shelf is None:
            shelf = Shelf(name=shelf_name)
            session.add(shelf)
            session.flush()
        session.add(LibraryEntryShelf(library_entry=entry, shelf=shelf))
        changed = True
    return changed


def _ensure_identifier(
    session: Session, edition: Edition, scheme: str, value: str
) -> bool:
    identifier = session.scalar(
        select(Identifier).where(Identifier.scheme == scheme, Identifier.value == value)
    )
    if identifier is not None:
        return False
    session.add(
        Identifier(edition=edition, scheme=scheme, value=value, source="goodreads_csv")
    )
    return True


def _record_goodreads_provenance(
    session: Session, import_record: ImportRecord, resolution: Resolution
) -> None:
    if resolution.work is None or resolution.edition is None:
        return
    for identifier in session.scalars(
        select(Identifier).where(
            Identifier.source == "goodreads_csv",
            (
                (Identifier.work_id == resolution.work.id)
                | (Identifier.edition_id == resolution.edition.id)
            ),
            Identifier.import_record_id.is_(None),
        )
    ):
        identifier.import_record_id = import_record.id

    for owner, field_name, value in (
        (resolution.work, "title", resolution.work.title),
        (
            resolution.work,
            "original_publication_year",
            resolution.work.original_publication_year,
        ),
        (resolution.edition, "title", resolution.edition.title),
        (resolution.edition, "format", resolution.edition.format),
        (resolution.edition, "publisher", resolution.edition.publisher),
        (
            resolution.edition,
            "publication_date",
            resolution.edition.publication_date,
        ),
        (
            resolution.edition,
            "publication_year",
            resolution.edition.publication_year,
        ),
        (resolution.edition, "page_count", resolution.edition.page_count),
        (resolution.edition, "language", resolution.edition.language),
    ):
        if value is None:
            continue
        work_id = owner.id if isinstance(owner, Work) else None
        edition_id = owner.id if isinstance(owner, Edition) else None
        existing = session.scalar(
            select(MetadataClaim).where(
                MetadataClaim.work_id == work_id,
                MetadataClaim.edition_id == edition_id,
                MetadataClaim.field_name == field_name,
                MetadataClaim.status == "accepted",
            )
        )
        if existing is not None:
            continue
        serialized = value.isoformat() if isinstance(value, date) else value
        session.add(
            MetadataClaim(
                work_id=work_id,
                edition_id=edition_id,
                field_name=field_name,
                value_json=serialized,
                normalized_value=str(serialized).casefold(),
                source_kind="goodreads_import",
                import_record_id=import_record.id,
                provider="goodreads_csv",
                confidence=1.0,
                status="accepted",
                selection_reason="Canonical value imported from Goodreads",
                observed_at=import_record.created_at,
            )
        )


def _set(entity: Any, attribute: str, value: Any) -> bool:
    if getattr(entity, attribute) == value:
        return False
    setattr(entity, attribute, value)
    return True


def _required(value: str, field: str, row_number: int) -> str:
    if not value.strip():
        raise GoodreadsFormatError(f"Row {row_number} is missing {field}")
    return value


def _clean_text(value: str) -> str:
    return " ".join(value.split())


def _optional_clean_text(value: str) -> str | None:
    cleaned = _clean_text(value)
    return cleaned or None


def _split_list(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _isbn(value: str, length: int, row_number: int) -> str | None:
    unwrapped = value[2:-1] if value.startswith('="') and value.endswith('"') else value
    normalized = re.sub(r"[-\s]", "", unwrapped).upper()
    if not normalized:
        return None
    pattern = r"\d{9}[\dX]" if length == 10 else r"\d{13}"
    if not re.fullmatch(pattern, normalized):
        raise GoodreadsFormatError(
            f"Row {row_number} has invalid ISBN-{length}: {value!r}"
        )
    return normalized


def _integer(value: str, field: str, row_number: int) -> int | None:
    if not value.strip():
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise GoodreadsFormatError(
            f"Row {row_number} has invalid integer in {field}: {value!r}"
        ) from exc


def _decimal(value: str, field: str, row_number: int) -> Decimal | None:
    if not value.strip():
        return None
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise GoodreadsFormatError(
            f"Row {row_number} has invalid decimal in {field}: {value!r}"
        ) from exc


def _date(value: str, field: str, row_number: int) -> date | None:
    if not value.strip():
        return None
    try:
        return date.fromisoformat(value.replace("/", "-"))
    except ValueError as exc:
        raise GoodreadsFormatError(
            f"Row {row_number} has invalid date in {field}: {value!r}"
        ) from exc


def _required_date(value: str, field: str, row_number: int) -> date:
    parsed = _date(value, field, row_number)
    if parsed is None:
        raise GoodreadsFormatError(f"Row {row_number} is missing {field}")
    return parsed


def _match_key(value: str) -> str:
    return _clean_text(value).casefold()


def _payload_checksum(payload: dict[str, str]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
