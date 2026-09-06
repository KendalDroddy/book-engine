"""Recommendation Center queries and idempotent feedback actions."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from book_engine.catalog.models import Author, Edition, Work, WorkAuthor
from book_engine.discovery.models import DiscoveryCandidate, DiscoveryRun
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
        recommended_for_you=RecommendationSection(
            title="Recommended for You",
            description=(
                "New books discovered beyond your library, ranked by fit and "
                "screened for candidate quality."
            ),
            run_id=best_run.id if best_run else None,
            cards=_cards(session, best_run, eligibility="eligible"),
            kind="discovered",
        ),
        want_to_read=RecommendationSection(
            title="Already on Your Radar",
            description=(
                "Books already on your Want to Read shelf, reordered by current fit. "
                "These are not newly discovered recommendations."
            ),
            run_id=want_run.id if want_run else None,
            cards=_cards(session, want_run),
            kind="want_to_read",
        ),
        withheld=_cards(session, best_run, eligibility="withheld"),
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
            "eligible_rank": item.eligible_rank,
            "display_eligible": item.display_eligible,
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
    session: Session,
    run: RecommendationRun | None,
    *,
    eligibility: str | None = None,
) -> tuple[RecommendationCard, ...]:
    if run is None:
        return ()
    statement = select(RecommendationItem).where(RecommendationItem.run_id == run.id)
    if eligibility == "eligible":
        statement = statement.where(RecommendationItem.display_eligible.is_(True))
        statement = statement.where(
            ~select(LibraryEntry.id)
            .where(LibraryEntry.work_id == RecommendationItem.work_id)
            .exists()
        )
        statement = statement.order_by(RecommendationItem.eligible_rank)
    elif eligibility == "withheld":
        statement = statement.where(RecommendationItem.display_eligible.is_(False))
        statement = statement.order_by(RecommendationItem.rank)
    else:
        statement = statement.order_by(RecommendationItem.rank)
    items = session.scalars(statement).all()
    discovery_run_id = run.configuration.get("source_reference", {}).get(
        "discovery_run_id"
    )
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
            (
                signal
                for signal in signals
                if signal.signal_name == "specific_trait_affinity"
            ),
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
            .where(RecommendationNeighbor.relationship_type == "semantic_neighbor")
            .order_by(RecommendationNeighbor.rank)
        ).all()
        explanation_record = session.scalar(
            select(RecommendationExplanation).where(
                RecommendationExplanation.recommendation_item_id == item.id
            )
        )
        explanation = (
            _concise_explanation(explanation_record.rendered_text)
            if explanation_record
            else "No explanation is available for this run."
        )
        structured_evidence = (
            explanation_record.structured_evidence if explanation_record else {}
        )
        discovery = None
        if isinstance(discovery_run_id, int):
            discovery = session.scalar(
                select(DiscoveryCandidate).where(
                    DiscoveryCandidate.run_id == discovery_run_id,
                    DiscoveryCandidate.work_id == work.id,
                )
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
                        signal.evidence_json,
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
                discovery_clusters=tuple(discovery.cluster_slugs) if discovery else (),
                strongest_specific_evidence=traits[:3],
                raw_rank=int(structured_evidence.get("raw_rank", item.rank)),
                diversified_rank=item.rank,
                eligible_rank=item.eligible_rank,
                display_eligible=item.display_eligible,
                eligibility_reasons=tuple(item.eligibility_reasons),
                eligibility_warnings=tuple(item.eligibility_warnings),
                eligibility_provenance=item.eligibility_provenance,
            )
        )
    return tuple(cards)


def _concise_explanation(value: str) -> str:
    excluded = (
        "Candidate-quality checks",
        "Confidence is limited",
        "It is withheld",
    )
    sentences = [
        sentence.strip()
        for sentence in value.split(".")
        if sentence.strip() and not sentence.strip().startswith(excluded)
    ]
    return ". ".join(sentences[:3]) + "."
