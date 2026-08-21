"""Single-work enrichment orchestration and provenance-aware merge rules."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from book_engine.catalog.models import Author, Edition, Identifier, Work, WorkAuthor
from book_engine.enrichment.matching import decide_match
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
from book_engine.enrichment.providers.openlibrary import PARSER_VERSION
from book_engine.enrichment.types import (
    BookLookup,
    BookMetadata,
    MatchDecision,
    MetadataProvider,
    ProviderError,
)


@dataclass(frozen=True)
class EnrichmentReport:
    run_id: int
    attempt_id: int
    work_id: int
    provider: str
    status: str
    decision: MatchDecision | None
    supplied_fields: tuple[str, ...]
    accepted_fields: tuple[str, ...]
    candidate_fields: tuple[str, ...]
    error: str | None = None


def enrich_work(
    session: Session,
    work_id: int,
    provider: MetadataProvider,
    *,
    refresh: bool = False,
) -> EnrichmentReport:
    lookup = _build_lookup(session, work_id)
    now = _utc_now()
    run = EnrichmentRun(
        provider=provider.name,
        mode="single",
        status="running",
        started_at=now,
        requested_count=1,
        configuration={"refresh": refresh},
    )
    attempt = EnrichmentAttempt(
        run_id=0,
        work_id=lookup.work_id,
        edition_id=lookup.edition_id,
        provider=provider.name,
        lookup_strategy=_lookup_strategy(lookup),
        lookup_key=lookup.request_key,
        status="running",
        started_at=now,
    )
    session.add(run)
    session.flush()
    attempt.run_id = run.id
    session.add(attempt)
    session.flush()

    cached = _cached_outcome(session, attempt, provider.name) if not refresh else None
    if cached is not None:
        attempt.status = "skipped_cached"
        attempt.decision_reason = f"Prior {cached} result is still authoritative"
        attempt.completed_at = _utc_now()
        run.skipped_count = 1
        _complete_run(run)
        session.commit()
        return EnrichmentReport(
            run_id=run.id,
            attempt_id=attempt.id,
            work_id=work_id,
            provider=provider.name,
            status="skipped_cached",
            decision=None,
            supplied_fields=(),
            accepted_fields=(),
            candidate_fields=(),
        )

    session.commit()

    try:
        search_result = provider.search(lookup)
    except ProviderError as exc:
        return _record_failure(session, run.id, attempt.id, provider.name, exc)
    except Exception as exc:
        wrapped = ProviderError(
            str(exc), request_key=lookup.request_key, endpoint="search"
        )
        return _record_failure(session, run.id, attempt.id, provider.name, wrapped)

    search_response = _store_response(
        session,
        attempt_id=attempt.id,
        provider=provider.name,
        operation="search",
        request_key=search_result.request_key,
        endpoint=search_result.endpoint,
        status_code=search_result.status_code,
        outcome="success",
        payload=search_result.raw_payload,
    )
    decision = decide_match(lookup, search_result.candidates)
    stored_attempt = session.get(EnrichmentAttempt, attempt.id)
    stored_run = session.get(EnrichmentRun, run.id)
    assert stored_attempt is not None and stored_run is not None
    _record_decision(session, stored_attempt, search_response, decision)

    if decision.status != "accepted" or decision.candidate is None:
        stored_attempt.status = decision.status
        stored_attempt.completed_at = _utc_now()
        if decision.status == "missed":
            stored_run.missed_count = 1
        else:
            stored_run.ambiguous_count = 1
        _complete_run(stored_run)
        session.commit()
        return EnrichmentReport(
            run_id=stored_run.id,
            attempt_id=stored_attempt.id,
            work_id=work_id,
            provider=provider.name,
            status=decision.status,
            decision=decision,
            supplied_fields=(),
            accepted_fields=(),
            candidate_fields=(),
        )

    session.commit()
    try:
        metadata_result = provider.fetch(decision.candidate)
    except ProviderError as exc:
        return _record_failure(
            session, run.id, attempt.id, provider.name, exc, decision=decision
        )
    except Exception as exc:
        wrapped = ProviderError(
            str(exc),
            request_key=f"work:{decision.candidate.external_work_id}",
            endpoint="fetch",
        )
        return _record_failure(
            session, run.id, attempt.id, provider.name, wrapped, decision=decision
        )

    detail_response = _store_response(
        session,
        attempt_id=attempt.id,
        provider=provider.name,
        operation="fetch",
        request_key=metadata_result.request_key,
        endpoint=metadata_result.endpoint,
        status_code=metadata_result.status_code,
        outcome="success",
        payload=metadata_result.raw_payload,
    )
    accepted_fields, candidate_fields = _merge_metadata(
        session,
        lookup,
        metadata_result.metadata,
        detail_response,
        decision.score,
    )
    stored_attempt = session.get(EnrichmentAttempt, attempt.id)
    stored_run = session.get(EnrichmentRun, run.id)
    assert stored_attempt is not None and stored_run is not None
    stored_attempt.status = "succeeded"
    stored_attempt.completed_at = _utc_now()
    stored_run.succeeded_count = 1
    _complete_run(stored_run)
    session.commit()

    return EnrichmentReport(
        run_id=stored_run.id,
        attempt_id=stored_attempt.id,
        work_id=work_id,
        provider=provider.name,
        status="succeeded",
        decision=decision,
        supplied_fields=_supplied_fields(metadata_result.metadata),
        accepted_fields=tuple(accepted_fields),
        candidate_fields=tuple(candidate_fields),
    )


def _build_lookup(session: Session, work_id: int) -> BookLookup:
    work = session.get(Work, work_id)
    if work is None:
        raise ValueError(f"Work {work_id} does not exist")
    edition = session.scalar(
        select(Edition).where(Edition.work_id == work.id).order_by(Edition.id)
    )
    if edition is None:
        raise ValueError(f"Work {work_id} has no edition")
    primary_author = session.scalar(
        select(Author.name)
        .join(WorkAuthor, WorkAuthor.author_id == Author.id)
        .where(WorkAuthor.work_id == work.id, WorkAuthor.position == 0)
    )
    if primary_author is None:
        raise ValueError(f"Work {work_id} has no primary author")
    identifiers = {
        item.scheme: item.value
        for item in session.scalars(
            select(Identifier).where(Identifier.edition_id == edition.id)
        )
    }
    return BookLookup(
        work_id=work.id,
        edition_id=edition.id,
        title=work.title,
        primary_author=primary_author,
        publication_year=work.original_publication_year,
        isbn10=identifiers.get("isbn10"),
        isbn13=identifiers.get("isbn13"),
    )


def _cached_outcome(
    session: Session, current_attempt: EnrichmentAttempt, provider: str
) -> str | None:
    prior = session.scalar(
        select(EnrichmentAttempt)
        .where(
            EnrichmentAttempt.work_id == current_attempt.work_id,
            EnrichmentAttempt.provider == provider,
            EnrichmentAttempt.id != current_attempt.id,
            EnrichmentAttempt.status.in_(("succeeded", "missed", "ambiguous")),
        )
        .order_by(EnrichmentAttempt.completed_at.desc())
    )
    return prior.status if prior is not None else None


def _store_response(
    session: Session,
    *,
    attempt_id: int,
    provider: str,
    operation: str,
    request_key: str,
    endpoint: str,
    status_code: int | None,
    outcome: str,
    payload: dict[str, Any] | None,
    error_message: str | None = None,
) -> ProviderResponse:
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if payload is not None
        else None
    )
    response = ProviderResponse(
        attempt_id=attempt_id,
        provider=provider,
        operation=operation,
        request_key=request_key,
        endpoint=endpoint,
        retrieved_at=_utc_now(),
        status_code=status_code,
        outcome=outcome,
        raw_payload=payload,
        payload_checksum=(
            hashlib.sha256(encoded.encode()).hexdigest()
            if encoded is not None
            else None
        ),
        parser_version=PARSER_VERSION,
        error_message=error_message,
    )
    session.add(response)
    session.flush()
    return response


def _record_decision(
    session: Session,
    attempt: EnrichmentAttempt,
    response: ProviderResponse,
    decision: MatchDecision,
) -> None:
    attempt.match_method = decision.method
    attempt.match_score = decision.score
    attempt.decision_reason = decision.reason
    equivalent_ids = {
        candidate.external_work_id for candidate in decision.equivalent_candidates
    }
    attempt.candidate_summary = [
        _evaluation_dict(
            item,
            relation=(
                "selected"
                if decision.candidate == item.candidate
                else "equivalent"
                if item.candidate.external_work_id in equivalent_ids
                else "rejected"
            ),
        )
        for item in decision.evaluations
    ]
    for evaluation in decision.evaluations:
        selected = decision.candidate == evaluation.candidate
        status = (
            "accepted"
            if selected
            else "ambiguous"
            if decision.status == "ambiguous"
            else "rejected"
        )
        session.add(
            MetadataMatch(
                attempt_id=attempt.id,
                provider_response_id=response.id,
                work_id=attempt.work_id,
                edition_id=attempt.edition_id,
                provider=attempt.provider,
                external_work_id=evaluation.candidate.external_work_id,
                external_edition_id=evaluation.candidate.external_edition_id,
                match_method=decision.method,
                match_score=evaluation.score,
                status=status,
                evidence=_evaluation_dict(
                    evaluation,
                    relation=(
                        "selected"
                        if selected
                        else "equivalent"
                        if evaluation.candidate.external_work_id in equivalent_ids
                        else "rejected"
                    ),
                ),
            )
        )


def _merge_metadata(
    session: Session,
    lookup: BookLookup,
    metadata: BookMetadata,
    response: ProviderResponse,
    confidence: float | None,
) -> tuple[list[str], list[str]]:
    work = session.get(Work, lookup.work_id)
    edition = session.get(Edition, lookup.edition_id)
    assert work is not None and edition is not None
    accepted: list[str] = []
    candidates: list[str] = []

    _merge_scalar(
        session,
        work,
        "title",
        metadata.title,
        response,
        confidence,
        accepted,
        candidates,
    )
    _merge_scalar(
        session,
        work,
        "subtitle",
        metadata.subtitle,
        response,
        confidence,
        accepted,
        candidates,
    )
    _merge_scalar(
        session,
        work,
        "description",
        metadata.description,
        response,
        confidence,
        accepted,
        candidates,
    )
    _merge_scalar(
        session,
        work,
        "original_publication_year",
        metadata.original_publication_year,
        response,
        confidence,
        accepted,
        candidates,
    )
    for field_name, value in (
        ("title", metadata.title),
        ("publisher", metadata.publisher),
        ("publication_date", metadata.publication_date),
        ("publication_year", metadata.publication_year),
        ("page_count", metadata.page_count),
        ("language", metadata.language),
    ):
        _merge_scalar(
            session,
            edition,
            field_name,
            value,
            response,
            confidence,
            accepted,
            candidates,
        )

    _ensure_identifier(
        session,
        work_id=work.id,
        edition_id=None,
        scheme="openlibrary_work_id",
        value=metadata.external_work_id,
        response=response,
    )
    if metadata.external_edition_id:
        _ensure_identifier(
            session,
            work_id=None,
            edition_id=edition.id,
            scheme="openlibrary_edition_id",
            value=metadata.external_edition_id,
            response=response,
        )
    for scheme, values in metadata.identifiers.items():
        for value in values:
            _ensure_identifier(
                session,
                work_id=None,
                edition_id=edition.id,
                scheme=scheme,
                value=value,
                response=response,
            )

    seen_subjects: set[str] = set()
    for label in metadata.subjects:
        normalized = _normalize_label(label)
        if normalized in seen_subjects:
            continue
        seen_subjects.add(normalized)
        concept = session.scalar(
            select(Concept).where(
                Concept.kind == "subject", Concept.normalized_label == normalized
            )
        )
        if concept is None:
            concept = Concept(kind="subject", label=label, normalized_label=normalized)
            session.add(concept)
            session.flush()
        session.add(
            WorkConceptClaim(
                work_id=work.id,
                concept_id=concept.id,
                provider_response_id=response.id,
                original_label=label,
                confidence=confidence,
                status="accepted",
            )
        )

    seen_series: set[str] = set()
    for series_name in metadata.series:
        normalized = _normalize_label(series_name)
        if normalized in seen_series:
            continue
        seen_series.add(normalized)
        series = session.scalar(
            select(Series).where(Series.normalized_name == normalized)
        )
        if series is None:
            series = Series(name=series_name, normalized_name=normalized)
            session.add(series)
            session.flush()
        session.add(
            WorkSeriesClaim(
                work_id=work.id,
                series_id=series.id,
                provider_response_id=response.id,
                confidence=confidence,
                status="accepted",
            )
        )

    if metadata.cover_id:
        preferred = edition.cover_url is None
        existing_cover = session.scalar(
            select(CoverCandidate).where(
                CoverCandidate.edition_id == edition.id,
                CoverCandidate.provider == response.provider,
                CoverCandidate.external_cover_id == metadata.cover_id,
            )
        )
        if existing_cover is None:
            session.add(
                CoverCandidate(
                    edition_id=edition.id,
                    provider_response_id=response.id,
                    provider=response.provider,
                    external_cover_id=metadata.cover_id,
                    small_url=metadata.cover_urls.get("small"),
                    medium_url=metadata.cover_urls.get("medium"),
                    large_url=metadata.cover_urls.get("large"),
                    status="preferred" if preferred else "candidate",
                    checked_at=_utc_now(),
                )
            )
        if metadata.cover_urls.get("medium"):
            _merge_scalar(
                session,
                edition,
                "cover_url",
                metadata.cover_urls["medium"],
                response,
                confidence,
                accepted,
                candidates,
            )
            if preferred:
                edition.cover_source = response.provider

    return accepted, candidates


def _merge_scalar(
    session: Session,
    entity: Work | Edition,
    field_name: str,
    value: Any,
    response: ProviderResponse,
    confidence: float | None,
    accepted: list[str],
    candidates: list[str],
) -> None:
    if value is None or value == "":
        return
    current = getattr(entity, field_name)
    status = "accepted" if current is None else "candidate"
    if status == "accepted":
        setattr(entity, field_name, value)
        accepted.append(field_name)
    else:
        candidates.append(field_name)
    serialized = value.isoformat() if isinstance(value, date) else value
    session.add(
        MetadataClaim(
            work_id=entity.id if isinstance(entity, Work) else None,
            edition_id=entity.id if isinstance(entity, Edition) else None,
            field_name=field_name,
            value_json=serialized,
            normalized_value=_normalized_value(serialized),
            source_kind="provider",
            provider_response_id=response.id,
            provider=response.provider,
            confidence=confidence,
            status=status,
            selection_reason=(
                "Filled an empty canonical field"
                if status == "accepted"
                else "Existing canonical value retained"
            ),
            observed_at=response.retrieved_at,
        )
    )


def _ensure_identifier(
    session: Session,
    *,
    work_id: int | None,
    edition_id: int | None,
    scheme: str,
    value: str,
    response: ProviderResponse,
) -> None:
    existing = session.scalar(
        select(Identifier).where(Identifier.scheme == scheme, Identifier.value == value)
    )
    if existing is not None:
        return
    session.add(
        Identifier(
            work_id=work_id,
            edition_id=edition_id,
            scheme=scheme,
            value=value,
            source=response.provider,
            provider_response_id=response.id,
        )
    )


def _record_failure(
    session: Session,
    run_id: int,
    attempt_id: int,
    provider: str,
    error: ProviderError,
    *,
    decision: MatchDecision | None = None,
) -> EnrichmentReport:
    run = session.get(EnrichmentRun, run_id)
    attempt = session.get(EnrichmentAttempt, attempt_id)
    assert run is not None and attempt is not None
    _store_response(
        session,
        attempt_id=attempt.id,
        provider=provider,
        operation="error",
        request_key=error.request_key,
        endpoint=error.endpoint,
        status_code=error.status_code,
        outcome="failed",
        payload=None,
        error_message=str(error),
    )
    attempt.status = "failed"
    attempt.error_type = type(error).__name__
    attempt.error_message = str(error)
    attempt.completed_at = _utc_now()
    attempt.next_retry_at = _utc_now() + timedelta(hours=1)
    run.failed_count = 1
    _complete_run(run)
    session.commit()
    return EnrichmentReport(
        run_id=run.id,
        attempt_id=attempt.id,
        work_id=attempt.work_id,
        provider=provider,
        status="failed",
        decision=decision,
        supplied_fields=(),
        accepted_fields=(),
        candidate_fields=(),
        error=str(error),
    )


def _complete_run(run: EnrichmentRun) -> None:
    run.completed_at = _utc_now()
    run.status = "completed_with_errors" if run.failed_count else "completed"


def _evaluation_dict(evaluation: Any, *, relation: str | None = None) -> dict[str, Any]:
    result = asdict(evaluation)
    result["candidate"] = asdict(evaluation.candidate)
    if relation is not None:
        result["relation"] = relation
    return result


def _supplied_fields(metadata: BookMetadata) -> tuple[str, ...]:
    fields = []
    for name, value in asdict(metadata).items():
        if value not in (None, "", (), {}):
            fields.append(name)
    return tuple(fields)


def _lookup_strategy(lookup: BookLookup) -> str:
    if lookup.isbn13:
        return "isbn13"
    if lookup.isbn10:
        return "isbn10"
    return "title_author"


def _normalize_label(value: str) -> str:
    return " ".join(value.casefold().split())


def _normalized_value(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.casefold().split())
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
