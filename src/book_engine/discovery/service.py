"""Bounded, cluster-diverse external candidate discovery."""

import hashlib
import json
import re
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from book_engine.catalog.models import Author, Edition, Identifier, Work, WorkAuthor
from book_engine.discovery.models import (
    DiscoveryCandidate,
    DiscoveryQuery,
    DiscoveryRun,
)
from book_engine.enrichment.providers.openlibrary import PARSER_VERSION
from book_engine.enrichment.service import ingest_known_candidate_metadata
from book_engine.enrichment.types import (
    DiscoveryProvider,
    MetadataCandidate,
    ProviderError,
)
from book_engine.library.models import LibraryEntry

STRATEGY_VERSION = "taste-cluster-search-v1"
CLUSTER_QUERIES = (
    ("systems-failure", 'subject:"industrial accidents"'),
    ("technical-narrative", 'subject:"technology" nonfiction'),
    ("war-military-history", 'subject:"military history"'),
    ("geopolitics", 'subject:"geopolitics"'),
    ("insider-access", 'subject:"personal narratives" institutions'),
    ("business-organizations", 'subject:"organizational behavior"'),
    ("creation-stories", 'subject:"entrepreneurship" history'),
    ("survival-exploration", 'subject:"survival" exploration'),
    ("historical-fiction", 'subject:"historical fiction"'),
    ("fantasy-science-fiction", '(subject:fantasy OR subject:"science fiction")'),
)


@dataclass(frozen=True)
class DiscoveryReport:
    run_id: int
    selected_count: int
    excluded_count: int
    enriched_count: int
    failed_count: int
    external_request_count: int
    cache_hit: bool


@dataclass
class _Aggregate:
    candidate: MetadataCandidate
    clusters: list[str]
    query_ids: list[int]


def discover_candidates(
    session: Session,
    provider: DiscoveryProvider,
    *,
    limit: int = 30,
) -> DiscoveryReport:
    if not 1 <= limit <= 100:
        raise ValueError("Discovery limit must be between 1 and 100")
    per_query = max(6, (limit + len(CLUSTER_QUERIES) - 1) // len(CLUSTER_QUERIES) * 2)
    configuration = {
        "limit": limit,
        "per_query": per_query,
        "clusters": [{"slug": slug, "query": query} for slug, query in CLUSTER_QUERIES],
    }
    input_hash = _stable_hash(configuration)
    existing = session.scalar(
        select(DiscoveryRun).where(
            DiscoveryRun.provider == provider.name,
            DiscoveryRun.strategy_version == STRATEGY_VERSION,
            DiscoveryRun.input_hash == input_hash,
        )
    )
    if (
        existing is not None
        and existing.selected_count > 0
        and existing.status in ("completed", "completed_with_errors")
    ):
        existing.cache_hit = True
        session.commit()
        return _report(session, existing, cache_hit=True)
    if existing is not None:
        session.execute(
            delete(DiscoveryCandidate).where(DiscoveryCandidate.run_id == existing.id)
        )
        session.execute(
            delete(DiscoveryQuery).where(DiscoveryQuery.run_id == existing.id)
        )
        existing.selected_count = 0
        existing.excluded_count = 0
        existing.external_request_count = 0
        existing.cache_hit = False
        existing.status = "running"
        existing.started_at = _now()
        existing.completed_at = None
        run = existing
    else:
        run = DiscoveryRun(
            provider=provider.name,
            strategy_version=STRATEGY_VERSION,
            input_hash=input_hash,
            requested_limit=limit,
            configuration=configuration,
            status="running",
            started_at=_now(),
        )
        session.add(run)
        session.flush()
    excluded_keys = _library_identity_keys(session, provider.name)
    pools: dict[str, deque[_Aggregate]] = {}
    aggregates: dict[str, _Aggregate] = {}
    errors = 0

    for cluster, query_text in CLUSTER_QUERIES:
        try:
            result = provider.discover(query_text, per_query)
            run.external_request_count += 1
            query = _store_query(session, run.id, cluster, query_text, result)
            pool: deque[_Aggregate] = deque()
            for candidate in result.candidates:
                identity = _candidate_identity(candidate, provider.name)
                if any(
                    key in excluded_keys
                    for key in _candidate_keys(candidate, provider.name)
                ):
                    run.excluded_count += 1
                    continue
                aggregate = aggregates.get(identity)
                if aggregate is None:
                    aggregate = _Aggregate(candidate, [], [])
                    aggregates[identity] = aggregate
                    pool.append(aggregate)
                if cluster not in aggregate.clusters:
                    aggregate.clusters.append(cluster)
                aggregate.query_ids.append(query.id)
            pools[cluster] = pool
        except ProviderError as exc:
            errors += 1
            run.external_request_count += 1
            session.add(
                DiscoveryQuery(
                    run_id=run.id,
                    cluster_slug=cluster,
                    query_text=query_text,
                    request_key=exc.request_key,
                    endpoint=exc.endpoint,
                    retrieved_at=_now(),
                    status_code=exc.status_code,
                    outcome="failed",
                    parser_version=PARSER_VERSION,
                    error_message=str(exc),
                )
            )
            pools[cluster] = deque()
    session.flush()

    selected: list[_Aggregate] = []
    seen: set[str] = set()
    while len(selected) < limit and any(pools.values()):
        for cluster, _ in CLUSTER_QUERIES:
            pool = pools[cluster]
            while (
                pool and _candidate_identity(pool[0].candidate, provider.name) in seen
            ):
                pool.popleft()
            if pool and len(selected) < limit:
                aggregate = pool.popleft()
                seen.add(_candidate_identity(aggregate.candidate, provider.name))
                selected.append(aggregate)

    for rank, aggregate in enumerate(selected, 1):
        work = _ensure_catalog_work(session, aggregate.candidate, provider.name)
        record = DiscoveryCandidate(
            run_id=run.id,
            work_id=work.id,
            provider=provider.name,
            external_work_id=aggregate.candidate.external_work_id,
            title=aggregate.candidate.title,
            primary_author=aggregate.candidate.authors[0],
            discovery_rank=rank,
            cluster_slugs=aggregate.clusters,
            query_ids=aggregate.query_ids,
            identifiers={
                key: list(values)
                for key, values in aggregate.candidate.identifiers.items()
            },
            source_metadata=asdict(aggregate.candidate),
            status="selected",
        )
        session.add(record)
        session.flush()
        try:
            run.external_request_count += 1
            detail = provider.fetch(aggregate.candidate)
            record.enrichment_attempt_id = ingest_known_candidate_metadata(
                session, work.id, provider.name, aggregate.candidate, detail
            )
            record.status = "enriched"
        except Exception as exc:
            record.status = "enrichment_failed"
            record.error_message = str(exc)
            errors += 1
        session.flush()

    run.selected_count = len(selected)
    run.status = (
        "failed"
        if not selected
        else "completed_with_errors"
        if errors
        else "completed"
    )
    run.completed_at = _now()
    session.commit()
    return _report(session, run, cache_hit=False)


def _store_query(
    session: Session, run_id: int, cluster: str, query_text: str, result: object
) -> DiscoveryQuery:
    from book_engine.enrichment.types import ProviderDiscoveryResult

    if not isinstance(result, ProviderDiscoveryResult):
        raise TypeError("result must be ProviderDiscoveryResult")
    encoded = json.dumps(result.raw_payload, sort_keys=True, separators=(",", ":"))
    query = DiscoveryQuery(
        run_id=run_id,
        cluster_slug=cluster,
        query_text=query_text,
        request_key=result.request_key,
        endpoint=result.endpoint,
        retrieved_at=_now(),
        status_code=result.status_code,
        outcome="success",
        result_count=len(result.candidates),
        raw_payload=result.raw_payload,
        payload_checksum=hashlib.sha256(encoded.encode()).hexdigest(),
        parser_version=PARSER_VERSION,
    )
    session.add(query)
    session.flush()
    return query


def _library_identity_keys(session: Session, provider: str) -> set[str]:
    rows = session.execute(
        select(Work.id, Work.title, Author.name, Identifier.value)
        .join(LibraryEntry, LibraryEntry.work_id == Work.id)
        .join(WorkAuthor, WorkAuthor.work_id == Work.id)
        .join(Author, Author.id == WorkAuthor.author_id)
        .outerjoin(
            Identifier,
            (Identifier.work_id == Work.id)
            & (Identifier.scheme == f"{provider}_work_id"),
        )
        .where(WorkAuthor.position == 0)
    ).all()
    keys: set[str] = set()
    for _, title, author, external_id in rows:
        keys.add(f"title-author:{_normalize(title)}|{_normalize(author)}")
        if external_id:
            keys.add(f"{provider}:{external_id}")
    return keys


def _candidate_identity(candidate: MetadataCandidate, provider: str) -> str:
    return (
        f"title-author:{_normalize(candidate.title)}|{_normalize(candidate.authors[0])}"
    )


def _candidate_keys(candidate: MetadataCandidate, provider: str) -> tuple[str, str]:
    return (
        f"{provider}:{candidate.external_work_id}",
        _candidate_identity(candidate, provider),
    )


def _ensure_catalog_work(
    session: Session, candidate: MetadataCandidate, provider: str
) -> Work:
    work = session.scalar(
        select(Work)
        .join(Identifier, Identifier.work_id == Work.id)
        .where(
            Identifier.scheme == f"{provider}_work_id",
            Identifier.value == candidate.external_work_id,
        )
    )
    if work is not None:
        return work
    work = Work(
        title=candidate.title,
        original_publication_year=candidate.publication_year,
        fiction_status="unknown",
    )
    session.add(work)
    session.flush()
    edition = Edition(
        work_id=work.id,
        title=candidate.title,
        publication_year=candidate.publication_year,
        cover_url=(
            f"https://covers.openlibrary.org/b/id/{candidate.cover_id}-M.jpg?default=false"
            if candidate.cover_id
            else None
        ),
        cover_source=provider if candidate.cover_id else None,
    )
    session.add(edition)
    author_name = candidate.authors[0]
    author = session.scalar(select(Author).where(Author.name == author_name))
    if author is None:
        author = Author(name=author_name, sort_name=author_name)
        session.add(author)
        session.flush()
    session.add(WorkAuthor(work_id=work.id, author_id=author.id, position=0))
    session.add(
        Identifier(
            work_id=work.id,
            scheme=f"{provider}_work_id",
            value=candidate.external_work_id,
            source=f"{provider}_discovery",
        )
    )
    session.flush()
    return work


def _report(session: Session, run: DiscoveryRun, *, cache_hit: bool) -> DiscoveryReport:
    candidates = session.scalars(
        select(DiscoveryCandidate).where(DiscoveryCandidate.run_id == run.id)
    ).all()
    return DiscoveryReport(
        run_id=run.id,
        selected_count=len(candidates),
        excluded_count=run.excluded_count,
        enriched_count=sum(item.status == "enriched" for item in candidates),
        failed_count=sum(item.status == "enrichment_failed" for item in candidates),
        external_request_count=0 if cache_hit else run.external_request_count,
        cache_hit=cache_hit,
    )


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _stable_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
