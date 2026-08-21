"""Reviewed mappings from provider concepts to useful browse facets."""

from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from book_engine.enrichment.models import Concept
from book_engine.web.models import BrowseFacet, ConceptFacetMapping


@dataclass(frozen=True)
class FacetDefinition:
    slug: str
    label: str
    category: str
    aliases: tuple[str, ...]


FACET_DEFINITIONS = (
    FacetDefinition("fiction", "Fiction", "form", ("fiction", "form:novel")),
    FacetDefinition("nonfiction", "Nonfiction", "form", ("nonfiction",)),
    FacetDefinition(
        "biography-memoir",
        "Biography & memoir",
        "form",
        ("biography", "autobiography", "memoir", "personal narratives"),
    ),
    FacetDefinition(
        "graphic-novels",
        "Graphic novels",
        "form",
        ("graphic novels", "comic books, strips", "comic books, strips, etc"),
    ),
    FacetDefinition(
        "fantasy",
        "Fantasy",
        "genre",
        (
            "fantasy",
            "fantasy fiction",
            "fantastic fiction",
            "fiction, fantasy, epic",
            "fiction, fantasy, general",
            "novela fantástica",
        ),
    ),
    FacetDefinition(
        "science-fiction",
        "Science fiction",
        "genre",
        (
            "science fiction",
            "genre:science fiction",
            "fiction, science fiction, general",
        ),
    ),
    FacetDefinition(
        "historical-fiction",
        "Historical fiction",
        "genre",
        (
            "historical fiction",
            "fiction, historical",
            "fiction, historical, general",
        ),
    ),
    FacetDefinition("mystery", "Mystery", "genre", ("mystery", "murder")),
    FacetDefinition("adventure", "Adventure", "genre", ("adventure",)),
    FacetDefinition(
        "history", "History", "subject", ("history", "historical")
    ),
    FacetDefinition(
        "world-war-ii",
        "World War II",
        "subject",
        ("world war, 1939-1945", "world war ii", "second world war"),
    ),
    FacetDefinition(
        "vietnam-war", "Vietnam War", "subject", ("vietnam war, 1961-1975",)
    ),
    FacetDefinition(
        "war-military", "War & military", "subject", ("war", "soldiers")
    ),
    FacetDefinition(
        "politics",
        "Politics",
        "subject",
        ("politics", "politics and government", "resistance to government"),
    ),
    FacetDefinition(
        "christianity",
        "Christianity",
        "subject",
        ("christianity", "christian life"),
    ),
    FacetDefinition(
        "geopolitics",
        "Geopolitics",
        "subject",
        ("geopolitics", "international relations"),
    ),
    FacetDefinition("sociology", "Sociology", "subject", ("sociology",)),
    FacetDefinition(
        "friendship", "Friendship", "theme", ("friendship",)
    ),
    FacetDefinition("loneliness", "Loneliness", "theme", ("loneliness",)),
    FacetDefinition("magic", "Magic", "theme", ("magic",)),
    FacetDefinition(
        "good-and-evil", "Good and evil", "theme", ("good and evil",)
    ),
)


def sync_browse_facets(session: Session) -> tuple[int, int]:
    """Upsert reviewed facets and rebuild deterministic concept mappings."""
    facets_by_slug: dict[str, BrowseFacet] = {}
    for order, definition in enumerate(FACET_DEFINITIONS):
        facet = session.scalar(
            select(BrowseFacet).where(BrowseFacet.slug == definition.slug)
        )
        if facet is None:
            facet = BrowseFacet(
                slug=definition.slug,
                label=definition.label,
                category=definition.category,
                display_order=order,
            )
            session.add(facet)
            session.flush()
        else:
            facet.label = definition.label
            facet.category = definition.category
            facet.display_order = order
        facets_by_slug[definition.slug] = facet

    session.execute(delete(ConceptFacetMapping))
    concepts = {
        concept.normalized_label: concept
        for concept in session.scalars(select(Concept)).all()
    }
    mapping_count = 0
    for definition in FACET_DEFINITIONS:
        for alias in definition.aliases:
            concept = concepts.get(alias)
            if concept is None:
                continue
            session.add(
                ConceptFacetMapping(
                    concept_id=concept.id,
                    facet_id=facets_by_slug[definition.slug].id,
                    mapping_rule="curated_alias",
                )
            )
            mapping_count += 1
    session.commit()
    return len(facets_by_slug), mapping_count
