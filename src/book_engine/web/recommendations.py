"""Recommendation Center queries and idempotent feedback actions."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from book_engine.catalog.models import Author, Edition, Work, WorkAuthor
from book_engine.discovery.models import DiscoveryRun
from book_engine.library.models import LibraryEntry
from book_engine.recommendations.models import (
    RecommendationExplanation,
    RecommendationFeedback,
    RecommendationItem,
    RecommendationNeighbor,
    RecommendationRun,
    RecommendationSignal,
)
from book_engine.web.viewmodels import (
    RecommendationCard,
    RecommendationCenter,
    RecommendationNeighborItem,
    RecommendationSection,
    RecommendationSignalItem,
)

FEEDBACK_ACTIONS = frozenset(
    {"add_to_want_to_read", "not_interested", "loved", "liked", "fine", "miss"}
)


def get_recommendation_center(session: Session) -> RecommendationCenter:
    best_run = _latest_run(session, "openlibrary_cluster_discovery")
    want_run = _latest_run(session, "existing_want_to_read_validation")
    latest_discovery = session.scalar(
        select(DiscoveryRun).order_by(DiscoveryRun.id.desc())
    )
    provider_message = None
    if latest_discovery is not None and latest_discovery.status == "failed":
        provider_message = (
            "External discovery is temporarily unavailable. "
            "Saved local recommendations remain available."
        )
    return RecommendationCenter(
        best_matches=RecommendationSection(
            title="Best Matches",
            description="New books discovered beyond your current library.",
            run_id=best_run.id if best_run else None,
            cards=_cards(session, best_run),
        ),
        want_to_read=RecommendationSection(
            title="Want to Read Ranked",
            description="Your existing Want to Read shelf, ordered by current fit.",
            run_id=want_run.id if want_run else None,
            cards=_cards(session, want_run),
        ),
        provider_message=provider_message,
    )


def record_recommendation_feedback(
    session: Session, item_id: int, action: str
) -> RecommendationFeedback:
    if action not in FEEDBACK_ACTIONS:
        raise ValueError("Unsupported recommendation feedback action")
    item = session.get(RecommendationItem, item_id)
    if item is None:
        raise ValueError("Recommendation item does not exist")
    existing = session.scalar(
        select(RecommendationFeedback).where(
            RecommendationFeedback.work_id == item.work_id,
            RecommendationFeedback.action == action,
        )
    )
    if existing is not None:
        return existing
    run = session.get(RecommendationRun, item.run_id)
    assert run is not None
    feedback = RecommendationFeedback(
        work_id=item.work_id,
        recommendation_item_id=item.id,
        action=action,
        source="recommendation_center",
        context_json={
            "recommendation_run_id": run.id,
            "algorithm_version": run.algorithm_version,
            "candidate_source": run.candidate_source,
            "rank": item.rank,
            "reranked_score": item.reranked_score,
            "confidence_label": item.confidence_label,
        },
    )
    session.add(feedback)
    if action == "add_to_want_to_read":
        library_entry = session.scalar(
            select(LibraryEntry).where(LibraryEntry.work_id == item.work_id)
        )
        if library_entry is None:
            session.add(
                LibraryEntry(
                    work_id=item.work_id,
                    status="want_to_read",
                    personal_rating=None,
                    personal_notes=None,
                    first_read_on=None,
                    last_read_on=None,
                )
            )
        elif library_entry.status not in ("read", "reading", "want_to_read"):
            library_entry.status = "want_to_read"
    session.commit()
    return feedback


def _latest_run(session: Session, source: str) -> RecommendationRun | None:
    return session.scalar(
        select(RecommendationRun)
        .where(
            RecommendationRun.candidate_source == source,
            RecommendationRun.status == "completed",
        )
        .order_by(RecommendationRun.completed_at.desc(), RecommendationRun.id.desc())
    )


def _cards(
    session: Session, run: RecommendationRun | None
) -> tuple[RecommendationCard, ...]:
    if run is None:
        return ()
    items = session.scalars(
        select(RecommendationItem)
        .where(RecommendationItem.run_id == run.id)
        .order_by(RecommendationItem.rank)
    ).all()
    cards: list[RecommendationCard] = []
    for item in items:
        work = session.get(Work, item.work_id)
        assert work is not None
        author = (
            session.scalar(
                select(Author.name)
                .join(WorkAuthor, WorkAuthor.author_id == Author.id)
                .where(WorkAuthor.work_id == work.id, WorkAuthor.position == 0)
            )
            or "Unknown author"
        )
        cover = session.scalar(
            select(Edition.cover_url)
            .where(Edition.work_id == work.id, Edition.cover_url.is_not(None))
            .order_by(Edition.id)
        )
        signals = session.scalars(
            select(RecommendationSignal)
            .where(RecommendationSignal.recommendation_item_id == item.id)
            .order_by(RecommendationSignal.contribution.desc())
        ).all()
        trait_signal = next(
            (signal for signal in signals if signal.signal_name == "trait_affinity"),
            None,
        )
        traits = (
            tuple(trait_signal.evidence_json.get("matched_traits", []))
            if trait_signal
            else ()
        )
        neighbors = session.execute(
            select(Work.title, RecommendationNeighbor.similarity)
            .join(
                RecommendationNeighbor, RecommendationNeighbor.read_work_id == Work.id
            )
            .where(RecommendationNeighbor.recommendation_item_id == item.id)
            .order_by(RecommendationNeighbor.rank)
        ).all()
        explanation = (
            session.scalar(
                select(RecommendationExplanation.rendered_text).where(
                    RecommendationExplanation.recommendation_item_id == item.id
                )
            )
            or "No explanation is available for this run."
        )
        feedback = frozenset(
            session.scalars(
                select(RecommendationFeedback.action).where(
                    RecommendationFeedback.work_id == work.id
                )
            ).all()
        )
        in_library = (
            session.scalar(
                select(LibraryEntry.id).where(LibraryEntry.work_id == work.id)
            )
            is not None
        )
        cards.append(
            RecommendationCard(
                item_id=item.id,
                work_id=work.id,
                title=work.title,
                author=author,
                cover_url=cover,
                score=item.reranked_score,
                match_label=item.match_label,
                confidence_label=item.confidence_label,
                repetitive=item.repetitive,
                matching_traits=traits,
                signals=tuple(
                    RecommendationSignalItem(
                        signal.signal_name,
                        signal.raw_value,
                        signal.weight,
                        signal.contribution,
                    )
                    for signal in signals
                ),
                neighbors=tuple(
                    RecommendationNeighborItem(title, similarity)
                    for title, similarity in neighbors
                ),
                explanation=explanation,
                feedback_actions=feedback,
                in_library=in_library,
            )
        )
    return tuple(cards)
