"""Conservative provider-neutral candidate matching."""

import re
import unicodedata
from difflib import SequenceMatcher

from book_engine.enrichment.types import (
    BookLookup,
    CandidateEvaluation,
    MatchDecision,
    MetadataCandidate,
)


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
    if len(isbn_matches) == 1:
        selected = isbn_matches[0]
        return MatchDecision(
            status="accepted",
            method="exact_isbn",
            score=selected.score,
            candidate=selected.candidate,
            evaluations=ranked,
            reason="Exact ISBN with consistent title and author",
        )
    if len(isbn_matches) > 1:
        return MatchDecision(
            status="ambiguous",
            method="exact_isbn",
            score=isbn_matches[0].score,
            candidate=None,
            evaluations=ranked,
            reason="Multiple non-conflicting candidates matched the same ISBN",
        )

    plausible = tuple(
        item
        for item in ranked
        if not item.conflicts
        and item.title_similarity >= 0.86
        and item.author_similarity >= 0.80
        and (
            (lookup.publication_year is None and item.year_difference is None)
            or (
                lookup.publication_year is not None
                and item.candidate.publication_year is not None
                and item.year_difference is not None
                and item.year_difference <= 3
            )
        )
    )
    if not plausible:
        return MatchDecision(
            status="missed",
            method="title_author_year",
            score=ranked[0].score,
            candidate=None,
            evaluations=ranked,
            reason="No candidate met conservative title, author, and year thresholds",
        )
    if len(plausible) > 1 and plausible[0].score - plausible[1].score < 0.08:
        return MatchDecision(
            status="ambiguous",
            method="title_author_year",
            score=plausible[0].score,
            candidate=None,
            evaluations=ranked,
            reason="Multiple candidates had similarly strong evidence",
        )

    selected = plausible[0]
    return MatchDecision(
        status="accepted",
        method="title_author_year",
        score=selected.score,
        candidate=selected.candidate,
        evaluations=ranked,
        reason="Unique candidate met conservative title, author, and year thresholds",
    )


def _evaluate(lookup: BookLookup, candidate: MetadataCandidate) -> CandidateEvaluation:
    title_similarity = _similarity(lookup.title, candidate.title)
    author_similarity = max(
        (_similarity(lookup.primary_author, author) for author in candidate.authors),
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

    conflicts: list[str] = []
    if isbn_match and title_similarity < 0.45:
        conflicts.append("ISBN matched but title was materially different")
    if isbn_match and author_similarity < 0.65:
        conflicts.append("ISBN matched but primary author was materially different")

    year_score = 0.5 if year_difference is None else max(0.0, 1 - year_difference / 10)
    score = (
        (0.55 if isbn_match else 0.0)
        + title_similarity * 0.22
        + author_similarity * 0.18
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
    )


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, _normalize(left), _normalize(right)).ratio()


def _normalize(value: str) -> str:
    value = re.sub(r"\s*\([^)]*#[^)]*\)\s*$", "", value)
    normalized = unicodedata.normalize("NFKD", value).casefold()
    return " ".join(re.findall(r"[a-z0-9]+", normalized))
