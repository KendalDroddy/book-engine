"""Cached semantic documents and a deterministic local baseline encoder."""

import hashlib
import json
import math
import re
import struct

from sqlalchemy import select
from sqlalchemy.orm import Session

from book_engine.catalog.models import Author, Work, WorkAuthor
from book_engine.enrichment.models import WorkConceptClaim
from book_engine.recommendations.models import WorkEmbedding, WorkRepresentation
from book_engine.recommendations.types import EmbeddingProvider, SemanticDocument
from book_engine.web.models import BrowseFacet, ConceptFacetMapping

REPRESENTATION_VERSION = "semantic-document-v1"
STOPWORDS = {
    "about",
    "after",
    "again",
    "against",
    "also",
    "among",
    "and",
    "are",
    "because",
    "been",
    "before",
    "being",
    "book",
    "but",
    "can",
    "from",
    "had",
    "has",
    "have",
    "her",
    "his",
    "into",
    "its",
    "more",
    "most",
    "not",
    "one",
    "only",
    "other",
    "our",
    "out",
    "over",
    "that",
    "the",
    "their",
    "them",
    "there",
    "these",
    "they",
    "this",
    "through",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "will",
    "with",
    "would",
    "you",
}


class HashingEmbeddingProvider:
    """Cheap lexical baseline implementing the replaceable embedding contract."""

    name = "local"
    model = "signed-hashing-v2"
    dimensions = 2048

    def embed(self, text: str) -> tuple[float, ...]:
        vector = [0.0] * self.dimensions
        tokens = [
            token
            for token in re.findall(r"[a-z0-9]+", text.casefold())
            if len(token) > 2 and token not in STOPWORDS
        ]
        features = tokens + [
            f"{left}_{right}" for left, right in zip(tokens, tokens[1:], strict=False)
        ]
        for feature in features:
            digest = hashlib.sha256(feature.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        magnitude = math.sqrt(sum(value * value for value in vector))
        if magnitude:
            vector = [value / magnitude for value in vector]
        return tuple(vector)


def build_semantic_document(session: Session, work_id: int) -> SemanticDocument:
    work = session.get(Work, work_id)
    if work is None:
        raise ValueError(f"Work {work_id} does not exist")
    author = session.scalar(
        select(Author.name)
        .join(WorkAuthor, WorkAuthor.author_id == Author.id)
        .where(WorkAuthor.work_id == work_id, WorkAuthor.position == 0)
    )
    facets = tuple(
        session.scalars(
            select(BrowseFacet.label)
            .join(
                ConceptFacetMapping,
                ConceptFacetMapping.facet_id == BrowseFacet.id,
            )
            .join(
                WorkConceptClaim,
                WorkConceptClaim.concept_id == ConceptFacetMapping.concept_id,
            )
            .where(
                WorkConceptClaim.work_id == work_id,
                WorkConceptClaim.status == "accepted",
            )
            .distinct()
            .order_by(BrowseFacet.display_order)
        ).all()
    )
    snapshot: dict[str, object] = {
        "title": work.title,
        "subtitle": work.subtitle,
        "author": author or "Unknown author",
        "description": work.description,
        "facets": facets,
        "publication_year": work.original_publication_year,
    }
    encoded = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    input_hash = hashlib.sha256(encoded.encode()).hexdigest()
    lines = [f"Title: {work.title}", f"Author: {author or 'Unknown author'}"]
    if work.subtitle and work.subtitle.casefold() not in work.title.casefold():
        lines.append(f"Subtitle: {work.subtitle}")
    if facets:
        lines.append(f"Curated concepts: {', '.join(facets)}")
    if work.description:
        lines.append(f"Description: {' '.join(work.description.split())}")
    return SemanticDocument(work_id, "\n".join(lines), input_hash, snapshot)


def ensure_representation(
    session: Session, work_id: int
) -> tuple[WorkRepresentation, bool]:
    document = build_semantic_document(session, work_id)
    existing = session.scalar(
        select(WorkRepresentation).where(
            WorkRepresentation.work_id == work_id,
            WorkRepresentation.kind == "recommendation_semantic_document",
            WorkRepresentation.builder_version == REPRESENTATION_VERSION,
            WorkRepresentation.input_hash == document.input_hash,
        )
    )
    if existing is not None:
        return existing, True
    representation = WorkRepresentation(
        work_id=work_id,
        kind="recommendation_semantic_document",
        content=document.content,
        input_hash=document.input_hash,
        builder_version=REPRESENTATION_VERSION,
        source_snapshot=document.source_snapshot,
    )
    session.add(representation)
    session.flush()
    return representation, False


def ensure_embedding(
    session: Session,
    representation: WorkRepresentation,
    provider: EmbeddingProvider,
) -> tuple[WorkEmbedding, bool]:
    existing = session.scalar(
        select(WorkEmbedding).where(
            WorkEmbedding.work_id == representation.work_id,
            WorkEmbedding.purpose == "recommendation_similarity",
            WorkEmbedding.provider == provider.name,
            WorkEmbedding.model == provider.model,
            WorkEmbedding.input_hash == representation.input_hash,
        )
    )
    if existing is not None:
        return existing, True
    vector = provider.embed(representation.content)
    embedding = WorkEmbedding(
        work_id=representation.work_id,
        representation_id=representation.id,
        purpose="recommendation_similarity",
        provider=provider.name,
        model=provider.model,
        dimensions=len(vector),
        vector_blob=pack_vector(vector),
        input_hash=representation.input_hash,
        representation_version=representation.builder_version,
    )
    session.add(embedding)
    session.flush()
    return embedding, False


def pack_vector(vector: tuple[float, ...]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_vector(value: bytes, dimensions: int) -> tuple[float, ...]:
    return struct.unpack(f"<{dimensions}f", value)


def cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def normalized_mean(vectors: list[tuple[float, ...]]) -> tuple[float, ...]:
    if not vectors:
        return ()
    values = [sum(column) / len(vectors) for column in zip(*vectors, strict=True)]
    magnitude = math.sqrt(sum(value * value for value in values))
    return tuple(value / magnitude for value in values) if magnitude else tuple(values)
