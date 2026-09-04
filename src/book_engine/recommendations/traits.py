"""Versioned, conservative recommendation traits derived from preserved metadata."""

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from book_engine.catalog.models import Work
from book_engine.enrichment.models import Concept, WorkConceptClaim
from book_engine.recommendations.models import TraitDefinition, WorkTraitValue
from book_engine.web.models import BrowseFacet, ConceptFacetMapping

TRAIT_SCHEMA_VERSION = "recommendation-traits-v2"
TRAIT_EXTRACTOR_VERSION = "curated-semantic-rules-v3"


@dataclass(frozen=True)
class SemanticTrait:
    slug: str
    label: str
    category: str
    description: str
    distinctive_phrases: tuple[str, ...]
    supporting_terms: tuple[str, ...] = ()


SEMANTIC_TRAITS = (
    SemanticTrait(
        "human-decision-making",
        "Human decision-making",
        "theme",
        "Choices under uncertainty, judgment, and leadership.",
        ("decision making", "human error", "command decisions", "moral choice"),
        (
            "decision",
            "decisions",
            "judgment",
            "leadership",
            "strategy",
            "crisis",
            "command",
        ),
    ),
    SemanticTrait(
        "systems-failure",
        "Systems failure",
        "theme",
        "How complex technical or institutional systems break down.",
        (
            "systems failure",
            "system failure",
            "engineering disaster",
            "organizational failure",
            "institutional failure",
        ),
        (
            "failure",
            "disaster",
            "collapse",
            "catastrophe",
            "accident",
            "crisis",
            "corruption",
        ),
    ),
    SemanticTrait(
        "technical-detail",
        "Technical detail",
        "depth",
        "Substantive scientific, engineering, operational, or craft detail.",
        (
            "technical detail",
            "how it works",
            "engineering",
            "computer science",
            "nuclear power",
            "space program",
        ),
        (
            "technology",
            "science",
            "technical",
            "design",
            "operations",
            "mechanics",
            "code",
        ),
    ),
    SemanticTrait(
        "insider-access",
        "Behind the scenes",
        "style",
        "First-hand or deeply reported access to institutions, teams, or events.",
        (
            "behind the scenes",
            "inside account",
            "insider account",
            "firsthand account",
            "first-hand account",
            "oral history",
        ),
        ("memoir", "diary", "reporter", "investigation", "inside", "eyewitness"),
    ),
    SemanticTrait(
        "escalating-tension",
        "Escalating tension",
        "style",
        "Narratives driven by accumulating pressure, danger, or uncertainty.",
        (
            "race against time",
            "escalating tension",
            "mounting tension",
            "fight for survival",
        ),
        (
            "crisis",
            "danger",
            "threat",
            "war",
            "battle",
            "murder",
            "conspiracy",
            "survive",
        ),
    ),
    SemanticTrait(
        "creation-stories",
        "Creation stories",
        "theme",
        "How organizations, technologies, creative works, or movements were built.",
        (
            "creation story",
            "origin story",
            "founding of",
            "making of",
            "how they built",
            "how it was built",
        ),
        (
            "founded",
            "founder",
            "startup",
            "invented",
            "created",
            "built",
            "entrepreneur",
        ),
    ),
    SemanticTrait(
        "survival",
        "Survival",
        "theme",
        "Physical or social endurance under severe pressure.",
        (
            "fight for survival",
            "struggle to survive",
            "survival story",
            "against all odds",
        ),
        ("survival", "survive", "shipwreck", "stranded", "ordeal", "wilderness"),
    ),
    SemanticTrait(
        "exploration",
        "Exploration",
        "theme",
        "Discovery through travel, expeditions, frontiers, or investigation.",
        (
            "age of exploration",
            "voyage of discovery",
            "polar expedition",
            "space exploration",
        ),
        (
            "exploration",
            "explorer",
            "expedition",
            "voyage",
            "discovery",
            "frontier",
            "journey",
        ),
    ),
    SemanticTrait(
        "geopolitics",
        "Geopolitics",
        "subject",
        "International power, geography, diplomacy, and state competition.",
        (
            "international relations",
            "foreign policy",
            "balance of power",
            "world order",
            "political geography",
        ),
        (
            "geopolitics",
            "diplomacy",
            "superpower",
            "empire",
            "nations",
            "global",
            "states",
        ),
    ),
    SemanticTrait(
        "business-organizational-systems",
        "Business & organizational systems",
        "subject",
        "How companies, institutions, incentives, and teams operate.",
        (
            "organizational culture",
            "corporate culture",
            "business strategy",
            "management system",
            "company culture",
        ),
        (
            "business",
            "company",
            "organization",
            "organizational",
            "management",
            "industry",
            "corporate",
            "team",
            "market",
        ),
    ),
)


def sync_recommendation_traits(
    session: Session, work_ids: list[int]
) -> dict[int, set[str]]:
    definitions = _definitions(session)
    facet_rows = session.execute(
        select(
            WorkConceptClaim.work_id,
            BrowseFacet.slug,
            BrowseFacet.label,
            BrowseFacet.id,
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
    ).all()
    facets_by_work: dict[int, list[tuple[str, str, int]]] = defaultdict(list)
    for work_id, slug, label, facet_id in facet_rows:
        facets_by_work[work_id].append((slug, label, facet_id))
    concept_rows = session.execute(
        select(WorkConceptClaim.work_id, Concept.id, Concept.label)
        .join(Concept, Concept.id == WorkConceptClaim.concept_id)
        .where(
            WorkConceptClaim.work_id.in_(work_ids),
            WorkConceptClaim.status == "accepted",
        )
        .distinct()
    ).all()
    concepts_by_work: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for work_id, concept_id, label in concept_rows:
        concepts_by_work[work_id].append((concept_id, label))

    result: dict[int, set[str]] = defaultdict(set)
    works = session.scalars(select(Work).where(Work.id.in_(work_ids))).all()
    for work in works:
        text = " ".join(
            filter(None, (work.title, work.subtitle, work.description))
        ).casefold()
        provider_concepts = concepts_by_work[work.id]
        trait_text = " ".join(
            [text, *(label.casefold() for _, label in provider_concepts)]
        )
        work_facets = facets_by_work[work.id]
        facet_slugs = {slug for slug, _, _ in work_facets}
        for slug, label, facet_id in work_facets:
            rejection = _facet_rejection(slug, facet_slugs, text)
            status = "rejected" if rejection else "accepted"
            if status == "accepted":
                result[work.id].add(slug)
            _store_value(
                session,
                work.id,
                definitions[slug],
                {"facet_id": facet_id, "content_hash": _hash(text)},
                0.85,
                status,
                {"facet": label, "cleanup_reason": rejection},
                {"browse_facet_id": facet_id},
            )
        for trait in SEMANTIC_TRAITS:
            phrases = [
                phrase
                for phrase in trait.distinctive_phrases
                if _contains(trait_text, phrase)
            ]
            terms = [
                term for term in trait.supporting_terms if _contains(trait_text, term)
            ]
            if not phrases and len(terms) < 2:
                continue
            result[work.id].add(trait.slug)
            _store_value(
                session,
                work.id,
                definitions[trait.slug],
                {
                    "content_hash": _hash(trait_text),
                    "phrases": phrases,
                    "terms": terms,
                },
                0.9 if phrases else min(0.85, 0.6 + len(terms) * 0.05),
                "accepted",
                {"matched_phrases": phrases, "matched_terms": terms},
                {
                    "fields": ["title", "subtitle", "description"],
                    "provider_concept_ids": [item[0] for item in provider_concepts],
                },
            )
    session.flush()
    return result


def trait_labels(session: Session) -> dict[str, str]:
    return {
        slug: label
        for slug, label in session.execute(
            select(TraitDefinition.slug, TraitDefinition.label)
        ).all()
    }


def _definitions(session: Session) -> dict[str, TraitDefinition]:
    facets = session.scalars(
        select(BrowseFacet).order_by(BrowseFacet.display_order)
    ).all()
    specs = [
        (f.slug, f.label, f.category, "Curated from normalized provider concepts")
        for f in facets
    ]
    specs.extend((t.slug, t.label, t.category, t.description) for t in SEMANTIC_TRAITS)
    result: dict[str, TraitDefinition] = {}
    for slug, label, category, description in specs:
        definition = session.scalar(
            select(TraitDefinition).where(TraitDefinition.slug == slug)
        )
        if definition is None:
            definition = TraitDefinition(
                slug=slug,
                label=label,
                category=category,
                value_type="boolean",
                description=description,
                schema_version=TRAIT_SCHEMA_VERSION,
                active=True,
            )
            session.add(definition)
            session.flush()
        else:
            definition.label = label
            definition.category = category
            definition.description = description
            definition.schema_version = TRAIT_SCHEMA_VERSION
            definition.active = True
        result[slug] = definition
    return result


def _facet_rejection(slug: str, slugs: set[str], text: str) -> str | None:
    if slug in {"fiction", "nonfiction"} and {"fiction", "nonfiction"} <= slugs:
        return "Conflicting provider form classifications"
    if slug == "graphic-novels" and not any(
        _contains(text, marker) for marker in ("graphic novel", "comic book", "comics")
    ):
        return "Provider graphic-novel subject is unsupported by title or description"
    return None


def _store_value(
    session: Session,
    work_id: int,
    definition: TraitDefinition,
    inputs: dict[str, object],
    confidence: float,
    status: str,
    evidence: dict[str, object],
    source_reference: dict[str, object],
) -> None:
    input_hash = _stable_hash(
        {
            "work_id": work_id,
            "trait": definition.slug,
            "version": TRAIT_EXTRACTOR_VERSION,
            **inputs,
        }
    )
    exists = session.scalar(
        select(WorkTraitValue.id).where(
            WorkTraitValue.work_id == work_id,
            WorkTraitValue.trait_id == definition.id,
            WorkTraitValue.source_kind == "deterministic",
            WorkTraitValue.extractor_version == TRAIT_EXTRACTOR_VERSION,
            WorkTraitValue.input_hash == input_hash,
        )
    )
    if exists is None:
        session.add(
            WorkTraitValue(
                work_id=work_id,
                trait_id=definition.id,
                value_text="present",
                confidence=confidence,
                source_kind="deterministic",
                source_reference=source_reference,
                extractor_version=TRAIT_EXTRACTOR_VERSION,
                input_hash=input_hash,
                evidence_json=evidence,
                status=status,
            )
        )


def _contains(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _stable_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
