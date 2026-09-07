"""Cached reputation collection and confidence-aware quality scoring."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from book_engine.catalog.models import Author, WorkAuthor
from book_engine.discovery.models import DiscoveryCandidate
from book_engine.recommendations.models import RecommendationItem, RecommendationRun
from book_engine.reputation.models import ReputationFetch, ReputationObservation
from book_engine.reputation.types import (
    ReputationCandidate,
    ReputationLookup,
    ReputationProvider,
)

MODEL_VERSION = "bayesian-quality-v1"
PRIOR_RATING = 3.8
PRIOR_WEIGHT = 1000
CONFIDENCE_HALF_SATURATION = 1000
MAX_REPUTATION_ADJUSTMENT = 8.0
FAILURE_RETRY_AFTER = timedelta(hours=1)


@dataclass(frozen=True)
class ReputationReport:
    discovery_run_id: int
    recommendation_run_id: int
    attempted: int
    succeeded: int
    missed: int
    ambiguous: int
    failed: int
    cache_hits: int
    external_requests: int


def enrich_discovery_reputation(
    session: Session,
    discovery_run_id: int,
    provider: ReputationProvider,
    *,
    force_refresh: bool = False,
) -> ReputationReport:
    recommendation_run = session.scalar(
        select(RecommendationRun)
        .where(
            RecommendationRun.candidate_source == "openlibrary_cluster_discovery",
            RecommendationRun.status == "completed",
            RecommendationRun.configuration["source_reference"][
                "discovery_run_id"
            ].as_integer()
            == discovery_run_id,
        )
        .order_by(RecommendationRun.completed_at.desc(), RecommendationRun.id.desc())
    )
    if recommendation_run is None:
        raise ValueError("No completed external recommendation run exists")
    referenced_run_id = recommendation_run.configuration.get(
        "source_reference", {}
    ).get("discovery_run_id")
    if referenced_run_id != discovery_run_id:
        raise ValueError(
            "Latest recommendation run does not reference this discovery run"
        )

    candidates = session.scalars(
        select(DiscoveryCandidate)
        .where(DiscoveryCandidate.run_id == discovery_run_id)
        .order_by(DiscoveryCandidate.discovery_rank)
    ).all()
    counts = {"succeeded": 0, "missed": 0, "ambiguous": 0, "failed": 0}
    cache_hits = 0
    external_requests = 0
    observations: dict[int, ReputationObservation] = {}
    for candidate in candidates:
        lookup = _lookup(session, candidate)
        input_hash = _stable_hash(
            {
                "provider": provider.name,
                "parser_version": provider.parser_version,
                "model_version": MODEL_VERSION,
                "title": lookup.title,
                "author": lookup.author,
                "isbns": lookup.isbns,
            }
        )
        existing_fetch = session.scalar(
            select(ReputationFetch)
            .where(
                ReputationFetch.provider == provider.name,
                ReputationFetch.input_hash == input_hash,
            )
            .order_by(ReputationFetch.fetched_at.desc(), ReputationFetch.id.desc())
        )
        if (
            not force_refresh
            and existing_fetch is not None
            and existing_fetch.status
            in {
                "succeeded",
                "missed",
                "ambiguous",
            }
        ):
            cache_hits += 1
            counts[existing_fetch.status] += 1
            observation = session.scalar(
                select(ReputationObservation).where(
                    ReputationObservation.fetch_id == existing_fetch.id
                )
            )
            if observation is not None:
                observations[candidate.work_id] = observation
            continue
        if (
            not force_refresh
            and existing_fetch is not None
            and existing_fetch.status == "failed"
            and _now() - existing_fetch.fetched_at < FAILURE_RETRY_AFTER
        ):
            cache_hits += 1
            counts["failed"] += 1
            continue

        fetched_at = _now()
        attempt_provenance: dict[str, object] = {
            "force_refresh": force_refresh,
            "previous_fetch_id": existing_fetch.id if existing_fetch else None,
        }
        try:
            external_requests += 1
            result = provider.lookup(lookup)
            external_requests += max(0, result.request_count - 1)
            decision = _select_candidate(lookup, result.candidates)
            status, selected, method, confidence, provenance = decision
            provenance.update(attempt_provenance)
            fetch = ReputationFetch(
                work_id=candidate.work_id,
                provider=provider.name,
                request_key=result.request_key,
                input_hash=input_hash,
                endpoint=result.endpoint,
                fetched_at=fetched_at,
                status=status,
                raw_response=result.raw_response,
                decision_provenance=provenance,
            )
            fetch.request_key = result.request_key
            fetch.endpoint = result.endpoint
            fetch.fetched_at = fetched_at
            fetch.status = status
            fetch.match_method = method
            fetch.match_confidence = confidence
            fetch.provider_book_id = selected.provider_book_id if selected else None
            fetch.raw_response = result.raw_response
            fetch.decision_provenance = provenance
            fetch.error_message = None
            session.add(fetch)
            session.flush()
            counts[status] += 1
            if selected is not None:
                observation = _observation(fetch, selected)
                session.add(observation)
                session.flush()
                observations[candidate.work_id] = observation
        except Exception as exc:
            prior_errors: list[object] = []
            if existing_fetch is not None:
                prior_errors = list(
                    existing_fetch.decision_provenance.get("prior_errors", [])
                )
                if existing_fetch.error_message:
                    prior_errors.append(existing_fetch.error_message)
            failed_fetch = ReputationFetch(
                work_id=candidate.work_id,
                provider=provider.name,
                request_key=f"work:{candidate.work_id}",
                input_hash=input_hash,
                endpoint="/volumes",
                fetched_at=fetched_at,
                status="failed",
                raw_response=None,
                decision_provenance={},
            )
            session.add(failed_fetch)
            failed_fetch.fetched_at = fetched_at
            failed_fetch.status = "failed"
            failed_fetch.decision_provenance = {
                "model_version": MODEL_VERSION,
                "prior_errors": prior_errors,
                **attempt_provenance,
            }
            failed_fetch.error_message = str(exc)
            counts["failed"] += 1
        session.flush()

    _apply_combined_scores(session, recommendation_run.id, observations)
    session.commit()
    return ReputationReport(
        discovery_run_id=discovery_run_id,
        recommendation_run_id=recommendation_run.id,
        attempted=len(candidates),
        succeeded=counts["succeeded"],
        missed=counts["missed"],
        ambiguous=counts["ambiguous"],
        failed=counts["failed"],
        cache_hits=cache_hits,
        external_requests=external_requests,
    )


def _lookup(session: Session, candidate: DiscoveryCandidate) -> ReputationLookup:
    author = (
        session.scalar(
            select(Author.name)
            .join(WorkAuthor, WorkAuthor.author_id == Author.id)
            .where(WorkAuthor.work_id == candidate.work_id, WorkAuthor.position == 0)
        )
        or candidate.primary_author
    )
    identifiers = candidate.identifiers or {}
    isbns = tuple(
        dict.fromkeys(
            value
            for scheme in ("isbn13", "isbn10")
            for value in identifiers.get(scheme, [])
            if isinstance(value, str)
        )
    )
    return ReputationLookup(candidate.work_id, candidate.title, author, isbns)


def _select_candidate(
    lookup: ReputationLookup, candidates: tuple[ReputationCandidate, ...]
) -> tuple[
    str,
    ReputationCandidate | None,
    str | None,
    float | None,
    dict[str, object],
]:
    evaluations: list[dict[str, object]] = []
    plausible: list[tuple[ReputationCandidate, str, float]] = []
    local_isbns = set(lookup.isbns)
    for candidate in candidates:
        isbn_match = bool(local_isbns & set(candidate.identifiers))
        title_similarity = _similarity(lookup.title, candidate.title)
        author_similarity = max(
            (_similarity(lookup.author, author) for author in candidate.authors),
            default=0.0,
        )
        method = "isbn" if isbn_match else "title_author"
        confidence = (
            1.0 if isbn_match else 0.65 * title_similarity + 0.35 * author_similarity
        )
        evaluations.append(
            {
                "provider_book_id": candidate.provider_book_id,
                "isbn_match": isbn_match,
                "title_similarity": round(title_similarity, 4),
                "author_similarity": round(author_similarity, 4),
                "match_confidence": round(confidence, 4),
                "average_rating": candidate.average_rating,
                "ratings_count": candidate.ratings_count,
            }
        )
        if isbn_match or (title_similarity >= 0.9 and author_similarity >= 0.8):
            plausible.append((candidate, method, confidence))
    rated = [
        item
        for item in plausible
        if item[0].average_rating is not None and item[0].ratings_count is not None
    ]
    provenance: dict[str, object] = {
        "model_version": MODEL_VERSION,
        "lookup": {
            "title": lookup.title,
            "author": lookup.author,
            "isbns": lookup.isbns,
        },
        "evaluations": evaluations,
    }
    if not rated:
        return "missed", None, None, None, provenance
    rated.sort(key=lambda item: (item[0].ratings_count or 0, item[2]), reverse=True)
    selected, method, confidence = rated[0]
    competing = [
        item
        for item in rated[1:]
        if item[0].title != selected.title
        and abs(item[2] - confidence) < 0.03
        and (item[0].ratings_count or 0) > (selected.ratings_count or 0) * 0.5
    ]
    if competing and method != "isbn":
        return "ambiguous", None, None, None, provenance
    provenance["selection_reason"] = (
        "highest rating count among identity-equivalent volumes"
    )
    return "succeeded", selected, method, confidence, provenance


def _observation(
    fetch: ReputationFetch, candidate: ReputationCandidate
) -> ReputationObservation:
    assert candidate.average_rating is not None and candidate.ratings_count is not None
    count = candidate.ratings_count
    posterior = (candidate.average_rating * count + PRIOR_RATING * PRIOR_WEIGHT) / (
        count + PRIOR_WEIGHT
    )
    confidence = count / (count + CONFIDENCE_HALF_SATURATION)
    score = max(0.0, min(100.0, 50.0 + (posterior - PRIOR_RATING) * 62.5))
    return ReputationObservation(
        fetch_id=fetch.id,
        work_id=fetch.work_id,
        provider=fetch.provider,
        provider_book_id=candidate.provider_book_id,
        average_rating=candidate.average_rating,
        ratings_count=count,
        normalized_score=round(score, 4),
        reputation_confidence=round(confidence, 6),
        label=_label(candidate.average_rating, count, score, confidence),
        model_version=MODEL_VERSION,
        model_provenance={
            "prior_rating": PRIOR_RATING,
            "prior_weight": PRIOR_WEIGHT,
            "confidence_half_saturation": CONFIDENCE_HALF_SATURATION,
            "posterior_rating": round(posterior, 6),
            "popularity_is_not_a_score_component": True,
        },
        fetched_at=fetch.fetched_at,
    )


def _apply_combined_scores(
    session: Session,
    recommendation_run_id: int,
    observations: dict[int, ReputationObservation],
) -> None:
    items = session.scalars(
        select(RecommendationItem).where(
            RecommendationItem.run_id == recommendation_run_id
        )
    ).all()
    work_ids = [item.work_id for item in items]
    available = session.scalars(
        select(ReputationObservation)
        .where(ReputationObservation.work_id.in_(work_ids))
        .order_by(
            ReputationObservation.fetched_at.desc(), ReputationObservation.id.desc()
        )
    ).all()
    seen: set[tuple[int, str, str, str]] = set()
    for available_observation in available:
        identity = (
            available_observation.work_id,
            available_observation.provider,
            available_observation.provider_book_id,
            available_observation.model_version,
        )
        if identity in seen:
            continue
        seen.add(identity)
        current = observations.get(available_observation.work_id)
        if current is None or (
            available_observation.reputation_confidence,
            available_observation.ratings_count,
        ) > (current.reputation_confidence, current.ratings_count):
            observations[available_observation.work_id] = available_observation
    for item in items:
        selected_observation = observations.get(item.work_id)
        adjustment = 0.0
        if selected_observation is not None:
            adjustment = (
                (selected_observation.normalized_score - 50.0)
                * selected_observation.reputation_confidence
                * (MAX_REPUTATION_ADJUSTMENT / 50.0)
            )
            item.reputation_observation_id = selected_observation.id
        else:
            item.reputation_observation_id = None
        item.reputation_adjustment = round(adjustment, 4)
        item.combined_score = round(item.reranked_score + adjustment, 4)
        item.combined_rank = None
    eligible = sorted(
        (item for item in items if item.display_eligible),
        key=lambda item: item.combined_score or item.reranked_score,
        reverse=True,
    )
    for rank, item in enumerate(eligible, 1):
        item.combined_rank = rank


def _label(average: float, count: int, score: float, confidence: float) -> str:
    if average >= 4.1 and count >= 10_000 and confidence >= 0.9:
        return "Proven Favorite"
    if score >= 60 and confidence >= 0.5:
        return "Well Reviewed"
    if score < 45 and confidence >= 0.5:
        return "Mixed Reviews"
    if average >= 4.0 and count < 500:
        return "Hidden Gem / Limited Rating Evidence"
    return "Low Reputation Confidence"


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, _normalize(left), _normalize(right)).ratio()


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _stable_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
