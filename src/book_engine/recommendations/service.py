"""Bounded, explainable positive-only recommendation validation workflow."""

import hashlib
import json
import math
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from book_engine.catalog.models import Author, Work, WorkAuthor
from book_engine.library.models import LibraryEntry
from book_engine.recommendations.models import (
    RecommendationExplanation,
    RecommendationItem,
    RecommendationNeighbor,
    RecommendationRun,
    RecommendationSignal,
    TasteProfileRun,
    TasteProfileValue,
)
from book_engine.recommendations.representation import (
    HashingEmbeddingProvider,
    cosine,
    ensure_embeddings,
    ensure_representation,
    normalized_mean,
    pack_vector,
    unpack_vector,
)
from book_engine.recommendations.traits import (
    TRAIT_EXTRACTOR_VERSION,
    sync_recommendation_traits,
    trait_labels,
)
from book_engine.recommendations.types import EmbeddingProvider

PROFILE_VERSION = "positive-only-profile-v2"
SCORING_VERSION = "hybrid-validation-v3"
ANCHOR_LIMIT = 18
SIGNAL_WEIGHTS = {
    "profile_similarity": 0.30,
    "trait_affinity": 0.25,
    "nearest_books": 0.25,
    "author_affinity": 0.05,
    "novelty": 0.10,
    "metadata_confidence": 0.05,
}


@dataclass(frozen=True)
class ValidationReport:
    run_id: int
    profile_run_id: int
    read_work_count: int
    represented_read_count: int
    candidate_count: int
    representation_cache_hits: int
    embedding_cache_hits: int
    cached_run: bool
    embedding_provider: str
    embedding_model: str
    derivation_run_id: int | None


def run_validation(
    session: Session, provider: EmbeddingProvider | None = None
) -> ValidationReport:
    read_ids = list(
        session.scalars(
            select(LibraryEntry.work_id)
            .where(LibraryEntry.status == "read")
            .order_by(LibraryEntry.work_id)
        ).all()
    )
    candidate_ids = list(
        session.scalars(
            select(LibraryEntry.work_id)
            .where(LibraryEntry.status == "want_to_read")
            .order_by(LibraryEntry.work_id)
        ).all()
    )
    if not read_ids or not candidate_ids:
        raise ValueError("Validation requires both read and want-to-read works")

    trait_map = sync_recommendation_traits(session, read_ids + candidate_ids)
    anchor_ids = _select_diverse_anchors(session, read_ids, trait_map)
    provider = provider or HashingEmbeddingProvider()
    vectors: dict[int, tuple[float, ...]] = {}
    representation_hits = 0
    embedding_hits = 0
    representation_hashes: dict[int, str] = {}
    representations = []
    for work_id in anchor_ids + candidate_ids:
        representation, representation_cached = ensure_representation(session, work_id)
        representation_hits += int(representation_cached)
        representation_hashes[work_id] = representation.input_hash
        representations.append(representation)
    embeddings, embedding_hits, derivation = ensure_embeddings(
        session, representations, provider
    )
    for work_id in anchor_ids + candidate_ids:
        embedding = embeddings[work_id]
        vectors[work_id] = unpack_vector(embedding.vector_blob, embedding.dimensions)

    profile_hash = _stable_hash(
        {
            "read_ids": read_ids,
            "anchors": anchor_ids,
            "traits": {str(key): sorted(value) for key, value in trait_map.items()},
            "representations": representation_hashes,
            "embedding_provider": provider.name,
            "embedding_model": provider.model,
            "version": PROFILE_VERSION,
        }
    )
    profile = session.scalar(
        select(TasteProfileRun).where(
            TasteProfileRun.algorithm_version == PROFILE_VERSION,
            TasteProfileRun.input_hash == profile_hash,
            TasteProfileRun.status == "completed",
        )
    )
    if profile is None:
        profile = _create_profile(
            session, read_ids, anchor_ids, trait_map, vectors, profile_hash
        )

    recommendation_hash = _stable_hash(
        {
            "profile_hash": profile_hash,
            "candidate_ids": candidate_ids,
            "candidate_representations": {
                str(key): representation_hashes[key] for key in candidate_ids
            },
            "weights": SIGNAL_WEIGHTS,
            "version": SCORING_VERSION,
        }
    )
    existing_run = session.scalar(
        select(RecommendationRun).where(
            RecommendationRun.algorithm_version == SCORING_VERSION,
            RecommendationRun.input_hash == recommendation_hash,
            RecommendationRun.status == "completed",
        )
    )
    if existing_run is not None:
        session.commit()
        return ValidationReport(
            existing_run.id,
            profile.id,
            len(read_ids),
            len(anchor_ids),
            len(candidate_ids),
            representation_hits,
            embedding_hits,
            True,
            provider.name,
            provider.model,
            None,
        )

    run = RecommendationRun(
        profile_run_id=profile.id,
        algorithm_version=SCORING_VERSION,
        input_hash=recommendation_hash,
        candidate_source="existing_want_to_read_validation",
        configuration={
            "weights": SIGNAL_WEIGHTS,
            "anchor_limit": ANCHOR_LIMIT,
            "ratings_used": False,
            "embedding_provider": provider.name,
            "embedding_model": provider.model,
        },
        status="running",
        started_at=_now(),
    )
    session.add(run)
    session.flush()
    _score_candidates(
        session, run, profile, read_ids, anchor_ids, candidate_ids, trait_map, vectors
    )
    run.status = "completed"
    run.completed_at = _now()
    session.commit()
    return ValidationReport(
        run.id,
        profile.id,
        len(read_ids),
        len(anchor_ids),
        len(candidate_ids),
        representation_hits,
        embedding_hits,
        False,
        provider.name,
        provider.model,
        derivation.id if derivation else None,
    )


def _select_diverse_anchors(
    session: Session, read_ids: list[int], traits: dict[int, set[str]]
) -> list[int]:
    descriptions = {
        work_id: description or ""
        for work_id, description in session.execute(
            select(Work.id, Work.description).where(Work.id.in_(read_ids))
        )
    }
    remaining = set(read_ids)
    selected: list[int] = []
    covered: set[str] = set()
    while remaining and len(selected) < ANCHOR_LIMIT:
        best = max(
            remaining,
            key=lambda work_id: (
                len(traits[work_id] - covered),
                len(traits[work_id]),
                min(len(descriptions[work_id]), 2000),
                -work_id,
            ),
        )
        selected.append(best)
        covered.update(traits[best])
        remaining.remove(best)
    return selected


def _create_profile(
    session: Session,
    read_ids: list[int],
    anchor_ids: list[int],
    traits: dict[int, set[str]],
    vectors: dict[int, tuple[float, ...]],
    input_hash: str,
) -> TasteProfileRun:
    centroid = normalized_mean([vectors[work_id] for work_id in anchor_ids])
    profile = TasteProfileRun(
        algorithm_version=PROFILE_VERSION,
        input_hash=input_hash,
        configuration={
            "ratings_used": False,
            "positive_status": "read",
            "semantic_anchor_limit": ANCHOR_LIMIT,
            "semantic_anchor_work_ids": anchor_ids,
            "trait_source": TRAIT_EXTRACTOR_VERSION,
        },
        source_work_count=len(read_ids),
        represented_work_count=len(anchor_ids),
        centroid_blob=pack_vector(centroid),
        dimensions=len(centroid),
        status="completed",
        started_at=_now(),
        completed_at=_now(),
    )
    session.add(profile)
    session.flush()
    counts = Counter(slug for work_id in read_ids for slug in traits[work_id])
    max_count = max(counts.values(), default=1)
    labels = trait_labels(session)
    for slug, count in counts.items():
        representatives = [work_id for work_id in read_ids if slug in traits[work_id]][
            :5
        ]
        session.add(
            TasteProfileValue(
                profile_run_id=profile.id,
                signal_kind="trait",
                signal_key=slug,
                label=labels[slug],
                weight=math.sqrt(count / max_count),
                support_count=count,
                confidence=min(1.0, count / 5),
                representative_work_ids=representatives,
            )
        )
    session.flush()
    return profile


def _score_candidates(
    session: Session,
    run: RecommendationRun,
    profile: TasteProfileRun,
    read_ids: list[int],
    anchor_ids: list[int],
    candidate_ids: list[int],
    traits: dict[int, set[str]],
    vectors: dict[int, tuple[float, ...]],
) -> None:
    assert profile.centroid_blob is not None and profile.dimensions is not None
    centroid = unpack_vector(profile.centroid_blob, profile.dimensions)
    profile_values = {
        value.signal_key: value
        for value in session.scalars(
            select(TasteProfileValue).where(
                TasteProfileValue.profile_run_id == profile.id
            )
        ).all()
    }
    author_counts = Counter(_primary_author(session, work_id) for work_id in read_ids)
    max_author_count = max(author_counts.values(), default=1)
    candidates: list[dict[str, object]] = []
    for work_id in candidate_ids:
        vector = vectors[work_id]
        neighbor_values = sorted(
            (
                (read_id, max(0.0, cosine(vector, vectors[read_id])))
                for read_id in anchor_ids
            ),
            key=lambda item: item[1],
            reverse=True,
        )[:3]
        profile_similarity = max(0.0, cosine(vector, centroid))
        nearest_score = statistics.fmean(value for _, value in neighbor_values)
        candidate_traits = traits[work_id]
        matched_traits = candidate_traits & profile_values.keys()
        trait_affinity = (
            statistics.fmean(profile_values[slug].weight for slug in matched_traits)
            if matched_traits
            else 0.0
        )
        author = _primary_author(session, work_id)
        author_affinity = author_counts[author] / max_author_count
        max_neighbor = neighbor_values[0][1]
        novelty = max(0.0, 1.0 - abs(max_neighbor - 0.55) / 0.55)
        metadata = _metadata_confidence(session, work_id, bool(candidate_traits))
        raw_signals = {
            "profile_similarity": profile_similarity,
            "trait_affinity": trait_affinity,
            "nearest_books": nearest_score,
            "author_affinity": author_affinity,
            "novelty": novelty,
            "metadata_confidence": metadata,
        }
        base_score = 100 * sum(
            raw_signals[name] * weight for name, weight in SIGNAL_WEIGHTS.items()
        )
        confidence = _score_confidence(raw_signals, metadata)
        candidates.append(
            {
                "work_id": work_id,
                "vector": vector,
                "neighbors": neighbor_values,
                "signals": raw_signals,
                "base_score": base_score,
                "confidence": confidence,
                "repetitive": max_neighbor >= 0.78 or author_affinity > 0,
            }
        )

    ordered: list[tuple[dict[str, object], float]] = []
    remaining = candidates.copy()
    while remaining:
        best_candidate: dict[str, object] | None = None
        best_reranked = float("-inf")
        for candidate in remaining:
            diversity_penalty = max(
                (
                    max(
                        0.0,
                        cosine(
                            cast(tuple[float, ...], candidate["vector"]),
                            cast(tuple[float, ...], selected["vector"]),
                        ),
                    )
                    for selected, _ in ordered
                ),
                default=0.0,
            )
            reranked = cast(float, candidate["base_score"]) - 8.0 * diversity_penalty
            if reranked > best_reranked:
                best_candidate = candidate
                best_reranked = reranked
        assert best_candidate is not None
        ordered.append((best_candidate, best_reranked))
        remaining.remove(best_candidate)

    labels = trait_labels(session)
    for rank, (candidate, reranked_score) in enumerate(ordered, 1):
        work_id = cast(int, candidate["work_id"])
        base_score = cast(float, candidate["base_score"])
        confidence = cast(float, candidate["confidence"])
        item = RecommendationItem(
            run_id=run.id,
            work_id=work_id,
            rank=rank,
            base_score=round(base_score, 4),
            reranked_score=round(reranked_score, 4),
            match_label=_match_label(reranked_score),
            confidence_score=round(confidence, 4),
            confidence_label=_confidence_label(confidence),
            repetitive=bool(candidate["repetitive"]),
        )
        session.add(item)
        session.flush()
        signals = cast(dict[str, float], candidate["signals"])
        for name, raw_value in signals.items():
            session.add(
                RecommendationSignal(
                    recommendation_item_id=item.id,
                    signal_name=name,
                    raw_value=round(raw_value, 6),
                    normalized_value=round(raw_value, 6),
                    weight=SIGNAL_WEIGHTS[name],
                    contribution=round(raw_value * SIGNAL_WEIGHTS[name] * 100, 4),
                    evidence_json=_signal_evidence(
                        name, work_id, traits, profile_values, labels
                    ),
                    algorithm_version=SCORING_VERSION,
                )
            )
        neighbors = cast(list[tuple[int, float]], candidate["neighbors"])
        for neighbor_rank, (read_work_id, similarity) in enumerate(neighbors, 1):
            session.add(
                RecommendationNeighbor(
                    recommendation_item_id=item.id,
                    read_work_id=read_work_id,
                    rank=neighbor_rank,
                    similarity=round(similarity, 6),
                    relationship_type="semantic_neighbor",
                )
            )
        explanation, evidence = _explanation(
            session,
            work_id,
            traits[work_id],
            profile_values,
            labels,
            neighbors,
            item.confidence_label,
            item.repetitive,
        )
        session.add(
            RecommendationExplanation(
                recommendation_item_id=item.id,
                explanation_type="structured_template",
                structured_evidence=evidence,
                rendered_text=explanation,
                generator="deterministic",
                generator_version="explanation-template-v2",
            )
        )


def _signal_evidence(
    name: str,
    work_id: int,
    traits: dict[int, set[str]],
    profile_values: dict[str, TasteProfileValue],
    labels: dict[str, str],
) -> dict[str, object]:
    if name == "trait_affinity":
        matched = [slug for slug in traits[work_id] if slug in profile_values]
        return {"matched_traits": [labels[slug] for slug in sorted(matched)]}
    if name == "metadata_confidence":
        return {"has_curated_traits": bool(traits[work_id])}
    return {}


def _explanation(
    session: Session,
    work_id: int,
    candidate_traits: set[str],
    profile_values: dict[str, TasteProfileValue],
    labels: dict[str, str],
    neighbors: list[tuple[int, float]],
    confidence: str,
    repetitive: bool,
) -> tuple[str, dict[str, object]]:
    matched = sorted(
        (slug for slug in candidate_traits if slug in profile_values),
        key=lambda slug: profile_values[slug].weight,
        reverse=True,
    )[:3]
    neighbor_titles = [
        session.scalar(select(Work.title).where(Work.id == read_id))
        for read_id, _ in neighbors[:2]
    ]
    parts: list[str] = []
    if matched:
        parts.append(
            "It matches your established interest in "
            + _human_join([labels[slug] for slug in matched])
            + "."
        )
    strongest_neighbor = neighbors[0][1] if neighbors else 0.0
    if neighbor_titles and strongest_neighbor >= 0.15:
        parts.append(
            "Its closest books in your reading history are "
            + _human_join([f"{title}" for title in neighbor_titles if title])
            + "."
        )
    elif neighbor_titles:
        parts.append("No strong semantic neighbor was found in the validation subset.")
    if repetitive:
        parts.append("It may feel familiar rather than exploratory.")
    if confidence == "low":
        parts.append("Confidence is limited because its metadata is sparse.")
    if not parts:
        parts.append("This is an exploratory candidate with limited matching evidence.")
    evidence: dict[str, object] = {
        "matched_traits": [labels[slug] for slug in matched],
        "neighbor_work_ids": [work_id for work_id, _ in neighbors[:2]],
        "confidence": confidence,
        "repetitive": repetitive,
    }
    return " ".join(parts), evidence


def _metadata_confidence(session: Session, work_id: int, has_traits: bool) -> float:
    work = session.get(Work, work_id)
    assert work is not None
    return min(
        1.0,
        0.45 * bool(work.description)
        + 0.35 * has_traits
        + 0.20 * bool(work.original_publication_year),
    )


def _score_confidence(signals: dict[str, float], metadata: float) -> float:
    core = [
        signals["profile_similarity"],
        signals["trait_affinity"],
        signals["nearest_books"],
    ]
    agreement = max(0.0, 1.0 - statistics.pstdev(core) * 2)
    evidence_strength = statistics.fmean(core)
    return 0.45 * metadata + 0.25 * agreement + 0.30 * evidence_strength


def _primary_author(session: Session, work_id: int) -> str:
    return (
        session.scalar(
            select(Author.name)
            .join(WorkAuthor, WorkAuthor.author_id == Author.id)
            .where(WorkAuthor.work_id == work_id, WorkAuthor.position == 0)
        )
        or "Unknown author"
    )


def _match_label(score: float) -> str:
    if score >= 55:
        return "strong"
    if score >= 30:
        return "promising"
    return "exploratory"


def _confidence_label(score: float) -> str:
    if score >= 0.72:
        return "high"
    if score >= 0.45:
        return "medium"
    return "low"


def _stable_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _human_join(values: list[str]) -> str:
    if len(values) < 2:
        return values[0] if values else ""
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    return f"{', '.join(values[:-1])}, and {values[-1]}"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
