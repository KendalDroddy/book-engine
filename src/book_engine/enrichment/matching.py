"""Conservative, work-centric provider candidate matching."""

import re
import unicodedata
from difflib import SequenceMatcher

from book_engine.enrichment.types import (
    BookLookup,
    CandidateEvaluation,
    MatchDecision,
    MetadataCandidate,
)

MIN_TITLE_SIMILARITY = 0.86
MIN_AUTHOR_SIMILARITY = 0.80
AMBIGUOUS_SCORE_GAP = 0.08


def decide_match(
    lookup: BookLookup, candidates: tuple[MetadataCandidate, ...]
) -> MatchDecision:
    evaluations = tuple(_evaluate(lookup, candidate) for candidate in candidates)
    if not evaluations:
        return MatchDecision(
            status="missed",
            method="none",
            score=None,
            candidate=None,
            evaluations=(),
            reason="Provider returned no candidates",
        )

    ranked = tuple(sorted(evaluations, key=lambda item: item.score, reverse=True))
    isbn_matches = tuple(
        item for item in ranked if item.isbn_match and not item.conflicts
    )
    if isbn_matches:
        return _decide_from_clusters(
            lookup,
            ranked,
            isbn_matches,
            method="exact_isbn",
            accepted_reason="Exact ISBN identifies a consistent intellectual work",
        )

    plausible = tuple(
        item
        for item in ranked
        if not item.conflicts
        and item.title_similarity >= MIN_TITLE_SIMILARITY
        and item.author_similarity >= MIN_AUTHOR_SIMILARITY
        and (
            item.year_difference is None
            or item.year_difference <= 15
            or item.title_similarity >= 0.96
        )
    )
    if not plausible:
        return MatchDecision(
            status="missed",
            method="work_signature",
            score=ranked[0].score,
            candidate=None,
            evaluations=ranked,
            reason="No candidate met conservative work-title and author requirements",
        )
    return _decide_from_clusters(
        lookup,
        ranked,
        plausible,
        method="work_signature",
        accepted_reason="Title and author identify the same intellectual work",
    )


def _decide_from_clusters(
    lookup: BookLookup,
    ranked: tuple[CandidateEvaluation, ...],
    plausible: tuple[CandidateEvaluation, ...],
    *,
    method: str,
    accepted_reason: str,
) -> MatchDecision:
    clusters: list[list[CandidateEvaluation]] = []
    for evaluation in plausible:
        for cluster in clusters:
            if any(_same_work_cluster(evaluation, item) for item in cluster):
                cluster.append(evaluation)
                break
        else:
            clusters.append([evaluation])

    ranked_clusters = sorted(
        clusters,
        key=lambda cluster: max(item.score for item in cluster),
        reverse=True,
    )
    if len(ranked_clusters) > 1:
        first_score = max(item.score for item in ranked_clusters[0])
        second_score = max(item.score for item in ranked_clusters[1])
        if first_score - second_score < AMBIGUOUS_SCORE_GAP:
            return MatchDecision(
                status="ambiguous",
                method=method,
                score=first_score,
                candidate=None,
                evaluations=ranked,
                reason="Multiple genuinely different work signatures remain plausible",
            )

    selected_cluster = ranked_clusters[0]
    representative = max(
        selected_cluster,
        key=lambda item: _representative_rank(lookup, item),
    )
    equivalents = tuple(
        item.candidate
        for item in selected_cluster
        if item.candidate != representative.candidate
    )
    duplicate_note = (
        f"; {len(equivalents)} duplicate provider work record(s) retained as equivalent"
        if equivalents
        else ""
    )
    return MatchDecision(
        status="accepted",
        method=method,
        score=representative.score,
        candidate=representative.candidate,
        evaluations=ranked,
        reason=f"{accepted_reason}{duplicate_note}",
        equivalent_candidates=equivalents,
    )


def _same_work_cluster(
    left: CandidateEvaluation, right: CandidateEvaluation
) -> bool:
    if left.work_signature == right.work_signature:
        return True
    return (
        left.title_similarity >= 0.96
        and right.title_similarity >= 0.96
        and left.author_similarity >= 0.95
        and right.author_similarity >= 0.95
    )


def _evaluate(lookup: BookLookup, candidate: MetadataCandidate) -> CandidateEvaluation:
    title_similarity = _title_similarity(lookup.title, candidate.title)
    author_similarity = max(
        (
            _text_similarity(lookup.primary_author, author)
            for author in candidate.authors
        ),
        default=0.0,
    )
    year_difference = (
        abs(lookup.publication_year - candidate.publication_year)
        if lookup.publication_year is not None
        and candidate.publication_year is not None
        else None
    )
    local_isbns = {value for value in (lookup.isbn10, lookup.isbn13) if value}
    candidate_isbns = {
        value
        for scheme in ("isbn10", "isbn13")
        for value in candidate.identifiers.get(scheme, ())
    }
    isbn_match = bool(local_isbns & candidate_isbns)

    conflicts = _variant_conflicts(lookup.title, candidate.title)
    if isbn_match and title_similarity < 0.55:
        conflicts.append("ISBN matched but work titles were materially different")
    if isbn_match and author_similarity < 0.70:
        conflicts.append("ISBN matched but primary authors were materially different")

    year_score = 0.5 if year_difference is None else max(0.0, 1 - year_difference / 25)
    score = (
        (0.55 if isbn_match else 0.0)
        + title_similarity * 0.25
        + author_similarity * 0.15
        + year_score * 0.05
    )
    return CandidateEvaluation(
        candidate=candidate,
        isbn_match=isbn_match,
        title_similarity=round(title_similarity, 4),
        author_similarity=round(author_similarity, 4),
        year_difference=year_difference,
        score=round(score, 4),
        conflicts=tuple(conflicts),
        work_signature=_work_signature(candidate.title, candidate.authors),
    )


def _representative_rank(
    lookup: BookLookup, evaluation: CandidateEvaluation
) -> tuple[int, int, int, int, float]:
    identifier_count = sum(
        len(values) for values in evaluation.candidate.identifiers.values()
    )
    year_rank = (
        -evaluation.year_difference
        if evaluation.year_difference is not None
        else -10_000
    )
    return (
        int(evaluation.isbn_match),
        year_rank,
        int(evaluation.candidate.cover_id is not None),
        identifier_count,
        evaluation.score,
    )


def _variant_conflicts(local_title: str, candidate_title: str) -> list[str]:
    local_variants = _variant_flags(local_title)
    candidate_variants = _variant_flags(candidate_title)
    new_variants = sorted(candidate_variants - local_variants)
    return [
        f"Candidate is a materially different publication variant: {variant}"
        for variant in new_variants
    ]


def _variant_flags(title: str) -> set[str]:
    normalized = _normalize_text(title)
    structural_text = unicodedata.normalize("NFKD", title).casefold()
    flags: set[str] = set()
    if re.search(
        r"\b\d+\s*(?:book|volume)s?\s+set\b|\bbox(?:ed)?\s+set\b|\bomnibus\b",
        normalized,
    ):
        flags.add("multi-work set or omnibus")
    if re.search(r"\b\d+\s*/\s*\d+\b", structural_text):
        flags.add("split-volume part")
    if re.search(
        r"\b(?:adaptation|retelling|study guide|summary|workbook|abridged)\b",
        normalized,
    ):
        flags.add("adaptation or derivative work")
    return flags


def _title_similarity(left: str, right: str) -> float:
    return max(
        _text_similarity(left_variant, right_variant)
        for left_variant in _title_variants(left)
        for right_variant in _title_variants(right)
    )


def _title_variants(value: str) -> tuple[str, ...]:
    without_series = re.sub(r"\s*\([^)]*#[^)]*\)\s*$", "", value).strip()
    variants = {_normalize_text(without_series)}
    if ":" in without_series:
        base_title = without_series.split(":", 1)[0]
        if len(_normalize_text(base_title).split()) >= 2:
            variants.add(_normalize_text(base_title))
    return tuple(item for item in variants if item)


def _work_signature(title: str, authors: tuple[str, ...]) -> str:
    title_variants = _title_variants(title)
    base_title = min(title_variants, key=lambda item: (len(item.split()), len(item)))
    primary_author = _normalize_text(authors[0]) if authors else ""
    return f"{base_title}|{primary_author}"


def _text_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, _normalize_text(left), _normalize_text(right)).ratio()


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).casefold()
    words = re.findall(r"[a-z0-9]+", normalized)
    if words and words[0] in {"a", "an", "the"}:
        words = words[1:]
    return " ".join(words)
