"""Read-only queries for the local library browser."""

import json
import math
import re
from collections import defaultdict
from typing import Any

from sqlalchemy import Select, exists, func, or_, select
from sqlalchemy.orm import Session

from book_engine.catalog.models import Author, Edition, Identifier, Work, WorkAuthor
from book_engine.enrichment.models import (
    Concept,
    EnrichmentAttempt,
    MetadataClaim,
    MetadataMatch,
    ProviderResponse,
    Series,
    WorkConceptClaim,
    WorkSeriesClaim,
)
from book_engine.library.models import (
    LibraryEntry,
    LibraryEntryShelf,
    ReadingEvent,
    Shelf,
)
from book_engine.web.models import BrowseFacet, ConceptFacetMapping
from book_engine.web.viewmodels import (
    BookCard,
    BookDetail,
    EditionItem,
    FacetItem,
    FilterOption,
    FilterOptions,
    LibraryFilters,
    LibraryPage,
    ProvenanceClaimItem,
    ProvenanceView,
    ReadingEventItem,
)

STATUS_LABELS = {
    "read": "Read",
    "reading": "Reading now",
    "want_to_read": "Want to read",
    "saved_recommendation": "Saved",
    "rejected": "Rejected",
}
SORTS = {"recent", "title", "author", "year_desc", "year_asc", "rating"}
NOISY_SUBJECT_PATTERNS = (
    r"new york times",
    r"large type",
    r"reading level",
    r"open library",
    r"accessible book",
    r"protected daisy",
    r"nyt:",
    r"^general$",
    r"^fiction$",
    r"^nonfiction$",
    r"literary collections",
    r"translations into",
    r"language$",
)


def get_library_page(session: Session, filters: LibraryFilters) -> LibraryPage:
    primary_author = (
        select(Author.name)
        .join(WorkAuthor, WorkAuthor.author_id == Author.id)
        .where(WorkAuthor.work_id == Work.id, WorkAuthor.position == 0)
        .order_by(WorkAuthor.position, Author.id)
        .limit(1)
        .correlate(Work)
        .scalar_subquery()
    )
    edition_id = (
        select(func.min(Edition.id))
        .where(Edition.work_id == Work.id)
        .correlate(Work)
        .scalar_subquery()
    )
    statement = (
        select(
            Work.id,
            Work.title,
            Work.description,
            Work.original_publication_year,
            Work.created_at,
            LibraryEntry.status,
            LibraryEntry.personal_rating,
            LibraryEntry.last_read_on,
            Edition.cover_url,
            primary_author.label("author"),
        )
        .join(LibraryEntry, LibraryEntry.work_id == Work.id)
        .outerjoin(Edition, Edition.id == edition_id)
    )
    statement = _apply_filters(statement, filters, primary_author)
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    statement = _apply_sort(statement, filters.sort, primary_author)
    total_pages = max(1, math.ceil(total / filters.per_page))
    page = min(filters.page, total_pages)
    rows = session.execute(
        statement.offset((page - 1) * filters.per_page).limit(filters.per_page)
    ).all()
    work_ids = [row.id for row in rows]
    shelves = _shelves_by_work(session, work_ids)
    facets = _facets_by_work(session, work_ids)
    books = tuple(
        BookCard(
            id=row.id,
            title=row.title,
            author=row.author or "Unknown author",
            status=row.status,
            rating=row.personal_rating,
            year=row.original_publication_year,
            last_read_on=row.last_read_on,
            cover_url=row.cover_url,
            description_excerpt=_excerpt(row.description),
            shelves=tuple(shelves[row.id]),
            facets=tuple(facets[row.id][:3]),
        )
        for row in rows
    )
    normalized_filters = LibraryFilters(**{**filters.__dict__, "page": page})
    return LibraryPage(
        books=books,
        filters=normalized_filters,
        options=_filter_options(session),
        total=total,
        page=page,
        total_pages=total_pages,
    )


def _apply_filters(
    statement: Select[Any], filters: LibraryFilters, primary_author: Any
) -> Select[Any]:
    if filters.q:
        term = f"%{_escape_like(filters.q.casefold())}%"
        statement = statement.where(
            or_(
                func.lower(Work.title).like(term, escape="\\"),
                func.lower(func.coalesce(Work.subtitle, "")).like(term, escape="\\"),
                func.lower(primary_author).like(term, escape="\\"),
            )
        )
    if filters.status:
        statement = statement.where(LibraryEntry.status == filters.status)
    if filters.shelf:
        statement = statement.where(
            exists(
                select(1)
                .select_from(LibraryEntryShelf)
                .join(Shelf, Shelf.id == LibraryEntryShelf.shelf_id)
                .where(
                    LibraryEntryShelf.library_entry_id == LibraryEntry.id,
                    Shelf.name == filters.shelf,
                )
            )
        )
    if filters.year_from is not None:
        statement = statement.where(
            Work.original_publication_year >= filters.year_from
        )
    if filters.year_to is not None:
        statement = statement.where(Work.original_publication_year <= filters.year_to)
    if filters.rating is not None:
        statement = statement.where(LibraryEntry.personal_rating == filters.rating)
    if filters.concept:
        statement = statement.where(
            exists(
                select(1)
                .select_from(WorkConceptClaim)
                .join(
                    ConceptFacetMapping,
                    ConceptFacetMapping.concept_id == WorkConceptClaim.concept_id,
                )
                .join(BrowseFacet, BrowseFacet.id == ConceptFacetMapping.facet_id)
                .where(
                    WorkConceptClaim.work_id == Work.id,
                    WorkConceptClaim.status == "accepted",
                    BrowseFacet.slug == filters.concept,
                )
            )
        )
    return statement


def _apply_sort(
    statement: Select[Any], sort: str, primary_author: Any
) -> Select[Any]:
    if sort == "title":
        return statement.order_by(func.lower(Work.title), Work.id)
    if sort == "author":
        return statement.order_by(func.lower(primary_author), func.lower(Work.title))
    if sort == "year_desc":
        return statement.order_by(
            Work.original_publication_year.is_(None),
            Work.original_publication_year.desc(),
            Work.title,
        )
    if sort == "year_asc":
        return statement.order_by(
            Work.original_publication_year.is_(None),
            Work.original_publication_year,
            Work.title,
        )
    if sort == "rating":
        return statement.order_by(
            LibraryEntry.personal_rating.is_(None),
            LibraryEntry.personal_rating.desc(),
            Work.title,
        )
    return statement.order_by(
        LibraryEntry.last_read_on.is_(None),
        LibraryEntry.last_read_on.desc(),
        Work.title,
    )


def _filter_options(session: Session) -> FilterOptions:
    status_rows = session.execute(
        select(LibraryEntry.status, func.count())
        .group_by(LibraryEntry.status)
        .order_by(LibraryEntry.status)
    ).all()
    shelf_rows = session.execute(
        select(Shelf.name, func.count(LibraryEntryShelf.library_entry_id))
        .join(LibraryEntryShelf, LibraryEntryShelf.shelf_id == Shelf.id)
        .group_by(Shelf.id)
        .order_by(Shelf.name)
    ).all()
    facet_rows = session.execute(
        select(
            BrowseFacet.slug,
            BrowseFacet.label,
            BrowseFacet.category,
            func.count(func.distinct(WorkConceptClaim.work_id)),
        )
        .join(ConceptFacetMapping, ConceptFacetMapping.facet_id == BrowseFacet.id)
        .join(
            WorkConceptClaim,
            WorkConceptClaim.concept_id == ConceptFacetMapping.concept_id,
        )
        .where(WorkConceptClaim.status == "accepted")
        .group_by(BrowseFacet.id)
        .order_by(BrowseFacet.category, BrowseFacet.display_order)
    ).all()
    min_year, max_year = session.execute(
        select(
            func.min(Work.original_publication_year),
            func.max(Work.original_publication_year),
        )
    ).one()
    return FilterOptions(
        statuses=tuple(
            FilterOption(value, STATUS_LABELS.get(value, value), count)
            for value, count in status_rows
        ),
        shelves=tuple(
            FilterOption(name, name.replace("-", " ").title(), count)
            for name, count in shelf_rows
        ),
        facets=tuple(
            FacetItem(slug, label, category, count)
            for slug, label, category, count in facet_rows
        ),
        min_year=min_year,
        max_year=max_year,
    )


def _shelves_by_work(session: Session, work_ids: list[int]) -> dict[int, list[str]]:
    result: dict[int, list[str]] = defaultdict(list)
    if not work_ids:
        return result
    rows = session.execute(
        select(LibraryEntry.work_id, Shelf.name)
        .join(LibraryEntryShelf, LibraryEntryShelf.library_entry_id == LibraryEntry.id)
        .join(Shelf, Shelf.id == LibraryEntryShelf.shelf_id)
        .where(LibraryEntry.work_id.in_(work_ids))
        .order_by(Shelf.name)
    )
    for work_id, name in rows:
        result[work_id].append(name)
    return result


def _facets_by_work(
    session: Session, work_ids: list[int]
) -> dict[int, list[FacetItem]]:
    result: dict[int, list[FacetItem]] = defaultdict(list)
    if not work_ids:
        return result
    rows = session.execute(
        select(
            WorkConceptClaim.work_id,
            BrowseFacet.slug,
            BrowseFacet.label,
            BrowseFacet.category,
        )
        .join(
            ConceptFacetMapping,
            ConceptFacetMapping.concept_id == WorkConceptClaim.concept_id,
        )
        .join(BrowseFacet, BrowseFacet.id == ConceptFacetMapping.facet_id)
        .where(
            WorkConceptClaim.work_id.in_(work_ids),
            WorkConceptClaim.status == "accepted",
        )
        .distinct()
        .order_by(BrowseFacet.display_order)
    )
    for work_id, slug, label, category in rows:
        result[work_id].append(FacetItem(slug, label, category))
    for work_id, items in result.items():
        slugs = {item.slug for item in items}
        if {"fiction", "nonfiction"}.issubset(slugs):
            result[work_id] = [
                item for item in items if item.slug not in {"fiction", "nonfiction"}
            ]
    return result


def get_book_detail(session: Session, work_id: int) -> BookDetail | None:
    work = session.get(Work, work_id)
    entry = session.scalar(
        select(LibraryEntry).where(LibraryEntry.work_id == work_id)
    )
    if work is None or entry is None:
        return None
    authors = tuple(
        session.scalars(
            select(Author.name)
            .join(WorkAuthor, WorkAuthor.author_id == Author.id)
            .where(WorkAuthor.work_id == work_id)
            .order_by(WorkAuthor.position, Author.id)
        ).all()
    )
    editions = session.scalars(
        select(Edition).where(Edition.work_id == work_id).order_by(Edition.id)
    ).all()
    edition_items = tuple(
        EditionItem(
            publisher=edition.publisher,
            publication_date=edition.publication_date,
            publication_year=edition.publication_year,
            page_count=edition.page_count,
            format=edition.format,
            language=edition.language,
            identifiers=tuple(
                (scheme, value)
                for scheme, value in session.execute(
                    select(Identifier.scheme, Identifier.value)
                    .where(Identifier.edition_id == edition.id)
                    .order_by(Identifier.scheme)
                )
            ),
        )
        for edition in editions
    )
    shelf_names = tuple(_shelves_by_work(session, [work_id])[work_id])
    facets = tuple(_facets_by_work(session, [work_id])[work_id])
    mapped_concepts = select(ConceptFacetMapping.concept_id)
    raw_subjects = session.scalars(
        select(Concept.label)
        .join(WorkConceptClaim, WorkConceptClaim.concept_id == Concept.id)
        .where(
            WorkConceptClaim.work_id == work_id,
            WorkConceptClaim.status == "accepted",
            Concept.id.not_in(mapped_concepts),
        )
        .distinct()
        .order_by(Concept.label)
    ).all()
    additional_subjects = tuple(
        label for label in raw_subjects if _presentable_subject(label)
    )[:18]
    series = tuple(
        session.scalars(
            select(Series.name)
            .join(WorkSeriesClaim, WorkSeriesClaim.series_id == Series.id)
            .where(
                WorkSeriesClaim.work_id == work_id,
                WorkSeriesClaim.status == "accepted",
            )
            .distinct()
            .order_by(Series.name)
        ).all()
    )
    events = tuple(
        ReadingEventItem(
            item.started_on,
            item.finished_on,
            item.date_precision,
            "Goodreads import" if item.source == "goodreads_csv" else item.source,
        )
        for item in session.scalars(
            select(ReadingEvent)
            .where(ReadingEvent.library_entry_id == entry.id)
            .order_by(ReadingEvent.finished_on.desc())
        ).all()
    )
    return BookDetail(
        id=work.id,
        title=work.title,
        subtitle=work.subtitle,
        authors=authors,
        description=work.description,
        original_publication_year=work.original_publication_year,
        language=work.language,
        fiction_status=work.fiction_status,
        cover_url=editions[0].cover_url if editions else None,
        status=entry.status,
        rating=entry.personal_rating,
        notes=entry.personal_notes,
        first_read_on=entry.first_read_on,
        last_read_on=entry.last_read_on,
        shelves=shelf_names,
        facets=facets,
        additional_subjects=additional_subjects,
        series=series,
        editions=edition_items,
        reading_events=events,
    )


def get_provenance(session: Session, work_id: int) -> ProvenanceView | None:
    work = session.get(Work, work_id)
    if work is None:
        return None
    edition_ids = select(Edition.id).where(Edition.work_id == work_id)
    claims = session.scalars(
        select(MetadataClaim)
        .where(
            or_(
                MetadataClaim.work_id == work_id,
                MetadataClaim.edition_id.in_(edition_ids),
            )
        )
        .order_by(MetadataClaim.field_name, MetadataClaim.observed_at.desc())
    ).all()
    attempt_ids = select(EnrichmentAttempt.id).where(
        EnrichmentAttempt.work_id == work_id
    )
    accepted_matches = session.scalar(
        select(func.count())
        .select_from(MetadataMatch)
        .where(MetadataMatch.work_id == work_id, MetadataMatch.status == "accepted")
    ) or 0
    responses = session.scalar(
        select(func.count())
        .select_from(ProviderResponse)
        .where(ProviderResponse.attempt_id.in_(attempt_ids))
    ) or 0
    concept_claims = session.scalar(
        select(func.count())
        .select_from(WorkConceptClaim)
        .where(WorkConceptClaim.work_id == work_id)
    ) or 0
    latest = session.scalar(
        select(func.max(EnrichmentAttempt.completed_at)).where(
            EnrichmentAttempt.work_id == work_id
        )
    )
    return ProvenanceView(
        work_id=work_id,
        title=work.title,
        claims=tuple(
            ProvenanceClaimItem(
                field_name=claim.field_name,
                value=_display_claim_value(claim.value_json),
                provider=claim.provider,
                source_kind=claim.source_kind,
                status=claim.status,
                reason=claim.selection_reason,
                observed_at=claim.observed_at,
            )
            for claim in claims
        ),
        accepted_matches=accepted_matches,
        provider_responses=responses,
        concept_claims=concept_claims,
        latest_enrichment_at=latest,
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _excerpt(value: str | None, length: int = 150) -> str | None:
    if not value:
        return None
    compact = " ".join(value.split())
    return compact if len(compact) <= length else f"{compact[: length - 1].rstrip()}…"


def _presentable_subject(label: str) -> bool:
    normalized = label.casefold()
    return (
        len(label) <= 80
        and re.match(r"^\d+(?:\.\d+)?(?:/\d+)?$", normalized) is None
        and not any(
        re.search(pattern, normalized) for pattern in NOISY_SUBJECT_PATTERNS
        )
    )


def _display_claim_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
