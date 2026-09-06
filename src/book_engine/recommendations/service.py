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
    TraitDefinition,
    WorkTraitValue,
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
SCORING_VERSION = "hybrid-validation-v5"
ANCHOR_LIMIT = 18
NEIGHBOR_EVIDENCE_THRESHOLD = 0.22
PROFILE_SIMILARITY_THRESHOLD = 0.18
SIGNAL_WEIGHTS = {
    "specific_trait_affinity": 0.28,
    "profile_similarity": 0.22,
    "nearest_books": 0.15,
    "discovery_alignment": 0.15,
    "broad_trait_affinity": 0.07,
    "novelty": 0.06,
    "metadata_confidence": 0.05,
    "author_affinity": 0.02,
}
BROAD_TRAITS = frozenset(
    {
        "fiction",
        "nonfiction",
        "history",
        "fantasy",
        "science-fiction",
        "historical-fiction",
        "biography-memoir",
        "politics",
    }
)
DISCOVERY_CLUSTER_TRAITS = {
    "systems-failure": frozenset({"systems-failure"}),
    "technical-narrative": frozenset({"technical-detail"}),
    "war-military-history": frozenset({"war-military", "world-war-ii", "vietnam-war"}),
    "geopolitics": frozenset({"geopolitics"}),
    "insider-access": frozenset({"insider-access"}),
    "business-organizations": frozenset({"business-organizational-systems"}),
    "creation-stories": frozenset({"creation-stories"}),
    "survival-exploration": frozenset({"survival", "exploration"}),
    "historical-fiction": frozenset({"historical-fiction"}),
    "fantasy-science-fiction": frozenset({"fantasy", "science-fiction"}),
}
DISCOVERY_CLUSTER_SUPPORTING_TRAITS = {
    "systems-failure": frozenset(
        {"human-decision-making", "technical-detail", "insider-access"}
    ),
    "technical-narrative": frozenset(
        {"human-decision-making", "insider-access", "systems-failure"}
    ),
    "war-military-history": frozenset(
        {"human-decision-making", "insider-access", "geopolitics"}
    ),
    "geopolitics": frozenset({"war-military", "human-decision-making"}),
    "insider-access": frozenset(
        {"human-decision-making", "technical-detail", "creation-stories"}
    ),
    "business-organizations": frozenset(
        {"human-decision-making", "creation-stories", "systems-failure"}
    ),
    "creation-stories": frozenset(
        {"business-organizational-systems", "technical-detail", "insider-access"}
    ),
    "survival-exploration": frozenset({"human-decision-making", "escalating-tension"}),
    "historical-fiction": frozenset(
        {"war-military", "geopolitics", "escalating-tension"}
    ),
    "fantasy-science-fiction": frozenset(
        {"adventure", "technical-detail", "escalating-tension"}
    ),
}
ALIGNMENT_VALUES = {
    "strong": 1.0,
    "moderate": 0.6,
    "weak": 0.25,
    "ambiguous": 0.0,
    "unsupported": -0.5,
    "not_applicable": 0.0,
}
CLUSTER_DIVERSITY_PENALTY = 2.5
MAX_CLUSTER_DIVERSITY_PENALTY = 5.0
ELIGIBILITY_VERSION = "candidate-display-eligibility-v1"


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
    candidate_ids = list(
        session.scalars(
            select(LibraryEntry.work_id)
            .where(LibraryEntry.status == "want_to_read")
            .order_by(LibraryEntry.work_id)
        ).all()
    )
    return _run_validation(
        session,
        candidate_ids,
        candidate_source="existing_want_to_read_validation",
        source_reference={},
        provider=provider,
    )


def run_discovery_validation(
    session: Session,
    discovery_run_id: int,
    provider: EmbeddingProvider | None = None,
) -> ValidationReport:
    from book_engine.discovery.models import DiscoveryCandidate, DiscoveryRun

    discovery_run = session.get(DiscoveryRun, discovery_run_id)
    if discovery_run is None:
        raise ValueError(f"Discovery run {discovery_run_id} does not exist")
    candidate_ids = list(
        session.scalars(
            select(DiscoveryCandidate.work_id)
            .where(DiscoveryCandidate.run_id == discovery_run_id)
            .order_by(DiscoveryCandidate.discovery_rank)
        ).all()
    )
    return _run_validation(
        session,
        candidate_ids,
        candidate_source="openlibrary_cluster_discovery",
        source_reference={
            "discovery_run_id": discovery_run_id,
            "discovery_input_hash": discovery_run.input_hash,
        },
        provider=provider,
    )


def _run_validation(
    session: Session,
    candidate_ids: list[int],
    *,
    candidate_source: str,
    source_reference: dict[str, object],
    provider: EmbeddingProvider | None,
) -> ValidationReport:
    read_ids = list(
        session.scalars(
            select(LibraryEntry.work_id)
            .where(LibraryEntry.status == "read")
            .order_by(LibraryEntry.work_id)
        ).all()
    )
    if not read_ids or not candidate_ids:
        raise ValueError("Validation requires both read works and candidates")

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
            "candidate_source": candidate_source,
            "source_reference": source_reference,
            "candidate_representations": {
                str(key): representation_hashes[key] for key in candidate_ids
            },
            "weights": SIGNAL_WEIGHTS,
            "version": SCORING_VERSION,
            "eligibility_version": ELIGIBILITY_VERSION,
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
        candidate_source=candidate_source,
        configuration={
            "weights": SIGNAL_WEIGHTS,
            "anchor_limit": ANCHOR_LIMIT,
            "ratings_used": False,
            "embedding_provider": provider.name,
            "embedding_model": provider.model,
            "source_reference": source_reference,
            "discovery_alignment_values": ALIGNMENT_VALUES,
            "cluster_diversity_penalty": CLUSTER_DIVERSITY_PENALTY,
            "max_cluster_diversity_penalty": MAX_CLUSTER_DIVERSITY_PENALTY,
            "eligibility_version": ELIGIBILITY_VERSION,
        },
        status="running",
        started_at=_now(),
    )
    session.add(run)
    session.flush()
    _score_candidates(
        session,
        run,
        profile,
        read_ids,
        anchor_ids,
        candidate_ids,
        trait_map,
        vectors,
        source_reference,
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
    source_reference: dict[str, object],
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
    discovery_clusters = _discovery_clusters(session, source_reference)
    trait_evidence = _trait_evidence(session, candidate_ids)
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
        raw_profile_similarity = max(0.0, cosine(vector, centroid))
        profile_similarity = _above_threshold(
            raw_profile_similarity, PROFILE_SIMILARITY_THRESHOLD
        )
        qualifying_neighbors = [
            value
            for _, value in neighbor_values
            if value >= NEIGHBOR_EVIDENCE_THRESHOLD
        ]
        nearest_score = (
            statistics.fmean(qualifying_neighbors) if qualifying_neighbors else 0.0
        )
        candidate_traits = traits[work_id]
        matched_traits = candidate_traits & profile_values.keys()
        broad_traits = matched_traits & BROAD_TRAITS
        specific_traits = matched_traits - BROAD_TRAITS
        broad_affinity = (
            statistics.fmean(profile_values[slug].weight for slug in broad_traits)
            * 0.25
            if broad_traits
            else 0.0
        )
        specific_affinity = (
            statistics.fmean(profile_values[slug].weight for slug in specific_traits)
            if specific_traits
            else 0.0
        )
        alignment, alignment_evidence = _discovery_alignment(
            discovery_clusters.get(work_id, ()),
            candidate_traits,
            trait_evidence.get(work_id, {}),
        )
        author = _primary_author(session, work_id)
        author_affinity = author_counts[author] / max_author_count
        max_neighbor = neighbor_values[0][1]
        novelty = max(0.0, 1.0 - abs(max_neighbor - 0.55) / 0.55)
        metadata = _metadata_confidence(session, work_id, bool(candidate_traits))
        raw_signals = {
            "profile_similarity": profile_similarity,
            "specific_trait_affinity": specific_affinity,
            "broad_trait_affinity": broad_affinity,
            "nearest_books": nearest_score,
            "discovery_alignment": alignment,
            "author_affinity": author_affinity,
            "novelty": novelty,
            "metadata_confidence": metadata,
        }
        base_score = 100 * sum(
            raw_signals[name] * weight for name, weight in SIGNAL_WEIGHTS.items()
        )
        confidence = _score_confidence(raw_signals, metadata)
        quality_flags = _candidate_quality_flags(
            session,
            work_id,
            discovery_clusters.get(work_id, ()),
            alignment_evidence,
        )
        eligibility = _display_eligibility(
            broad_only=bool(broad_traits and not specific_traits),
            specific_affinity=specific_affinity,
            profile_similarity=profile_similarity,
            nearest_score=nearest_score,
            alignment=alignment,
            alignment_evidence=alignment_evidence,
            quality_flags=quality_flags,
        )
        candidates.append(
            {
                "work_id": work_id,
                "vector": vector,
                "neighbors": neighbor_values,
                "signals": raw_signals,
                "base_score": base_score,
                "confidence": confidence,
                "repetitive": max_neighbor >= 0.78 or author_affinity > 0,
                "alignment_evidence": alignment_evidence,
                "broad_only": bool(broad_traits and not specific_traits),
                "quality_flags": quality_flags,
                "eligibility": eligibility,
            }
        )

    raw_ordered = sorted(
        candidates,
        key=lambda candidate: cast(float, candidate["base_score"]),
        reverse=True,
    )
    raw_ranks = {
        cast(int, candidate["work_id"]): rank
        for rank, candidate in enumerate(raw_ordered, 1)
    }
    ordered: list[tuple[dict[str, object], float]] = []
    remaining = candidates.copy()
    selected_cluster_counts: Counter[str] = Counter()
    while remaining:
        best_candidate: dict[str, object] | None = None
        best_reranked = float("-inf")
        for candidate in remaining:
            clusters = discovery_clusters.get(cast(int, candidate["work_id"]), ())
            prior_cluster_count = max(
                (selected_cluster_counts[cluster] for cluster in clusters), default=0
            )
            diversity_penalty = min(
                MAX_CLUSTER_DIVERSITY_PENALTY,
                CLUSTER_DIVERSITY_PENALTY * prior_cluster_count,
            )
            reranked = cast(float, candidate["base_score"]) - diversity_penalty
            if reranked > best_reranked:
                best_candidate = candidate
                best_reranked = reranked
        assert best_candidate is not None
        ordered.append((best_candidate, best_reranked))
        selected_cluster_counts.update(
            discovery_clusters.get(cast(int, best_candidate["work_id"]), ())
        )
        remaining.remove(best_candidate)

    labels = trait_labels(session)
    eligible_rank = 0
    for rank, (candidate, reranked_score) in enumerate(ordered, 1):
        work_id = cast(int, candidate["work_id"])
        base_score = cast(float, candidate["base_score"])
        confidence = cast(float, candidate["confidence"])
        eligibility = cast(dict[str, object], candidate["eligibility"])
        display_eligible = cast(bool, eligibility["eligible"])
        if display_eligible:
            eligible_rank += 1
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
            display_eligible=display_eligible,
            eligible_rank=eligible_rank if display_eligible else None,
            eligibility_reasons=cast(list[str], eligibility["reasons"]),
            eligibility_warnings=cast(list[str], eligibility["warnings"]),
            eligibility_provenance=cast(dict[str, object], eligibility["provenance"]),
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
                        name,
                        work_id,
                        traits,
                        profile_values,
                        labels,
                        cast(dict[str, object], candidate["alignment_evidence"]),
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
                    relationship_type=(
                        "semantic_neighbor"
                        if similarity >= NEIGHBOR_EVIDENCE_THRESHOLD
                        else "weak_semantic_neighbor"
                    ),
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
            bool(candidate["broad_only"]),
            cast(dict[str, object], candidate["alignment_evidence"]),
            cast(list[dict[str, object]], candidate["quality_flags"]),
            eligibility,
            raw_ranks[work_id],
            rank,
        )
        session.add(
            RecommendationExplanation(
                recommendation_item_id=item.id,
                explanation_type="structured_template",
                structured_evidence=evidence,
                rendered_text=explanation,
                generator="deterministic",
                generator_version="explanation-template-v3",
            )
        )


def _signal_evidence(
    name: str,
    work_id: int,
    traits: dict[int, set[str]],
    profile_values: dict[str, TasteProfileValue],
    labels: dict[str, str],
    alignment_evidence: dict[str, object],
) -> dict[str, object]:
    if name in {"specific_trait_affinity", "broad_trait_affinity"}:
        matched = {slug for slug in traits[work_id] if slug in profile_values}
        matched = (
            matched & BROAD_TRAITS
            if name.startswith("broad")
            else matched - BROAD_TRAITS
        )
        return {"matched_traits": [labels[slug] for slug in sorted(matched)]}
    if name == "discovery_alignment":
        return alignment_evidence
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
    broad_only: bool,
    alignment_evidence: dict[str, object],
    quality_flags: list[dict[str, object]],
    eligibility: dict[str, object],
    raw_rank: int,
    display_rank: int,
) -> tuple[str, dict[str, object]]:
    matched = sorted(
        (slug for slug in candidate_traits if slug in profile_values),
        key=lambda slug: profile_values[slug].weight,
        reverse=True,
    )[:3]
    parts: list[str] = []
    if matched:
        parts.append(
            "It matches your established interest in "
            + _human_join([labels[slug] for slug in matched])
            + "."
        )
    strong_neighbors = [
        (read_id, score)
        for read_id, score in neighbors
        if score >= NEIGHBOR_EVIDENCE_THRESHOLD
    ]
    neighbor_titles = [
        session.scalar(select(Work.title).where(Work.id == read_id))
        for read_id, _ in strong_neighbors[:2]
    ]
    if neighbor_titles:
        parts.append(
            "Its closest books in your reading history are "
            + _human_join([f"{title}" for title in neighbor_titles if title])
            + "."
        )
    elif neighbors:
        parts.append("No strong semantic neighbor was found in the validation subset.")
    if broad_only:
        parts.append("Its trait overlap is limited to broad categories.")
    if alignment_evidence.get("supported") is False:
        parts.append("Its enriched metadata does not support its discovery hypothesis.")
    elif alignment_evidence.get("strength") in {"weak", "ambiguous"}:
        parts.append(
            "Its discovery hypothesis has only "
            f"{alignment_evidence['strength']} metadata support."
        )
    if quality_flags:
        parts.append("Candidate-quality checks found limited supporting data.")
    if not eligibility["eligible"]:
        parts.append("It is withheld from the primary recommendation list.")
    if repetitive:
        parts.append("It may feel familiar rather than exploratory.")
    if confidence == "low":
        parts.append("Confidence is limited because predictive evidence is weak.")
    if not parts:
        parts.append("This is an exploratory candidate with limited matching evidence.")
    evidence: dict[str, object] = {
        "matched_traits": [labels[slug] for slug in matched],
        "neighbor_work_ids": [work_id for work_id, _ in strong_neighbors[:2]],
        "weak_neighbor_work_ids": [
            work_id
            for work_id, score in neighbors
            if score < NEIGHBOR_EVIDENCE_THRESHOLD
        ],
        "confidence": confidence,
        "repetitive": repetitive,
        "broad_category_only": broad_only,
        "discovery_alignment": alignment_evidence,
        "quality_flags": quality_flags,
        "display_eligibility": eligibility,
        "raw_rank": raw_rank,
        "display_rank": display_rank,
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
    alignment = max(0.0, signals["discovery_alignment"])
    return min(
        1.0,
        0.15 * metadata
        + 0.35 * signals["specific_trait_affinity"]
        + 0.25 * signals["profile_similarity"]
        + 0.15 * signals["nearest_books"]
        + 0.10 * alignment,
    )


def _above_threshold(value: float, threshold: float) -> float:
    if value < threshold:
        return 0.0
    return min(1.0, (value - threshold) / (1.0 - threshold))


def _discovery_clusters(
    session: Session, source_reference: dict[str, object]
) -> dict[int, tuple[str, ...]]:
    from book_engine.discovery.models import DiscoveryCandidate

    run_id = source_reference.get("discovery_run_id")
    if not isinstance(run_id, int):
        return {}
    rows = session.execute(
        select(DiscoveryCandidate.work_id, DiscoveryCandidate.cluster_slugs).where(
            DiscoveryCandidate.run_id == run_id
        )
    ).all()
    return {work_id: tuple(cluster_slugs or ()) for work_id, cluster_slugs in rows}


def _trait_evidence(
    session: Session, work_ids: list[int]
) -> dict[int, dict[str, list[dict[str, object]]]]:
    rows = session.execute(
        select(WorkTraitValue, TraitDefinition.slug)
        .join(TraitDefinition, TraitDefinition.id == WorkTraitValue.trait_id)
        .where(
            WorkTraitValue.work_id.in_(work_ids),
            WorkTraitValue.status == "accepted",
        )
    ).all()
    result: dict[int, dict[str, list[dict[str, object]]]] = {}
    for value, slug in rows:
        result.setdefault(value.work_id, {}).setdefault(slug, []).append(
            {
                "confidence": value.confidence,
                "source_kind": value.source_kind,
                "extractor_version": value.extractor_version,
                "evidence": value.evidence_json,
            }
        )
    return result


def _discovery_alignment(
    clusters: tuple[str, ...],
    candidate_traits: set[str],
    trait_evidence: dict[str, list[dict[str, object]]] | None = None,
) -> tuple[float, dict[str, object]]:
    trait_evidence = trait_evidence or {}
    expected = set().union(
        *(DISCOVERY_CLUSTER_TRAITS.get(cluster, frozenset()) for cluster in clusters)
    )
    matched = expected & candidate_traits
    supporting = set().union(
        *(
            DISCOVERY_CLUSTER_SUPPORTING_TRAITS.get(cluster, frozenset())
            for cluster in clusters
        )
    )
    matched_supporting = supporting & candidate_traits
    applicable = bool(expected)
    supported = bool(matched) if applicable else None
    records = [record for slug in matched for record in trait_evidence.get(slug, [])]
    phrase_count = sum(
        _evidence_list_length(record, "matched_phrases") for record in records
    )
    term_count = sum(
        _evidence_list_length(record, "matched_terms") for record in records
    )
    if not applicable:
        strength = "not_applicable"
    elif not matched:
        strength = "unsupported"
    elif matched <= BROAD_TRAITS:
        strength = "moderate" if len(matched) > 1 else "ambiguous"
    elif len(matched) > 1 or len(matched_supporting) >= 2:
        strength = "strong"
    elif matched_supporting or phrase_count or term_count >= 3:
        strength = "moderate"
    else:
        strength = "weak"
    evidence: dict[str, object] = {
        "applicable": applicable,
        "clusters": list(clusters),
        "expected_traits": sorted(expected),
        "matched_traits": sorted(matched),
        "supporting_traits": sorted(matched_supporting),
        "trait_evidence": {
            slug: trait_evidence.get(slug, []) for slug in sorted(matched)
        },
        "phrase_count": phrase_count,
        "term_count": term_count,
        "strength": strength,
        "supported": supported,
    }
    return ALIGNMENT_VALUES[strength], evidence


def _evidence_list_length(record: dict[str, object], key: str) -> int:
    evidence = record.get("evidence")
    if not isinstance(evidence, dict):
        return 0
    values = evidence.get(key)
    return len(values) if isinstance(values, list) else 0


def _candidate_quality_flags(
    session: Session,
    work_id: int,
    clusters: tuple[str, ...],
    alignment_evidence: dict[str, object],
) -> list[dict[str, object]]:
    from book_engine.discovery.models import DiscoveryCandidate

    work = session.get(Work, work_id)
    candidate = session.scalar(
        select(DiscoveryCandidate).where(
            DiscoveryCandidate.work_id == work_id,
            DiscoveryCandidate.cluster_slugs == list(clusters),
        )
    )
    if work is None or candidate is None:
        return []
    flags: list[dict[str, object]] = []
    description_length = len((work.description or "").strip())
    if description_length < 120:
        flags.append(
            {
                "code": "sparse_metadata",
                "description_length": description_length,
            }
        )
    normalized_title = " ".join(
        part for part in work.title.casefold().replace("/", " ").split() if part
    )
    title_words = normalized_title.split()
    if len(title_words) == 1 or normalized_title in {
        "the company",
        "the explorer",
        "you 2",
    }:
        flags.append({"code": "generic_title", "title": work.title})
    identifiers = candidate.identifiers or {}
    has_isbn = bool(identifiers.get("isbn10") or identifiers.get("isbn13"))
    if not candidate.external_work_id or not has_isbn:
        flags.append(
            {
                "code": "weak_identity",
                "external_work_id": candidate.external_work_id,
                "has_isbn": has_isbn,
            }
        )
    if alignment_evidence.get("strength") == "weak":
        flags.append(
            {
                "code": "single_low_information_discovery_trait",
                "matched_traits": alignment_evidence.get("matched_traits", []),
            }
        )
    flag_codes = {cast(str, flag["code"]) for flag in flags}
    title_has_non_ascii = any(ord(character) > 127 for character in work.title)
    if (
        alignment_evidence.get("strength") == "unsupported"
        and "sparse_metadata" in flag_codes
        and title_has_non_ascii
    ):
        flags.append(
            {
                "code": "catalog_language_noise",
                "language": work.language,
                "heuristic": "unsupported_sparse_non_ascii_title",
            }
        )
    return flags


def _display_eligibility(
    *,
    broad_only: bool,
    specific_affinity: float,
    profile_similarity: float,
    nearest_score: float,
    alignment: float,
    alignment_evidence: dict[str, object],
    quality_flags: list[dict[str, object]],
) -> dict[str, object]:
    flag_codes = {cast(str, flag["code"]) for flag in quality_flags}
    unsupported = alignment_evidence.get("strength") == "unsupported"
    no_semantic_evidence = profile_similarity == 0.0 and nearest_score == 0.0
    strong_positive_evidence = specific_affinity >= 0.35 and (
        alignment >= ALIGNMENT_VALUES["moderate"] or nearest_score >= 0.22
    )
    reasons: list[str] = []
    if "catalog_language_noise" in flag_codes:
        reasons.append("catalog_or_language_noise_with_unsupported_discovery")
    if unsupported and (
        "sparse_metadata" in flag_codes or broad_only or no_semantic_evidence
    ):
        reasons.append("unsupported_discovery_compounded_by_weak_evidence")
    if "weak_identity" in flag_codes and (
        "sparse_metadata" in flag_codes or no_semantic_evidence
    ):
        reasons.append("weak_identity_compounded_by_limited_metadata")
    if (
        "generic_title" in flag_codes
        and no_semantic_evidence
        and alignment <= ALIGNMENT_VALUES["weak"]
        and not strong_positive_evidence
    ):
        reasons.append("generic_title_without_qualifying_support")
    if broad_only and no_semantic_evidence and alignment <= 0:
        reasons.append("broad_category_only_without_qualifying_support")

    warnings = sorted(flag_codes)
    provenance: dict[str, object] = {
        "version": ELIGIBILITY_VERSION,
        "inputs": {
            "alignment_strength": alignment_evidence.get("strength"),
            "broad_category_only": broad_only,
            "specific_affinity": round(specific_affinity, 6),
            "profile_similarity": round(profile_similarity, 6),
            "nearest_score": round(nearest_score, 6),
            "quality_flag_codes": warnings,
        },
        "rules": {
            "requires_compounded_quality_problems": True,
            "sparse_metadata_alone_withheld": False,
            "strong_positive_evidence": strong_positive_evidence,
        },
    }
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "warnings": warnings,
        "provenance": provenance,
    }


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
