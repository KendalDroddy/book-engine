"""Cached semantic documents and a deterministic local baseline encoder."""

import hashlib
import json
import math
import re
import struct
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from book_engine.catalog.models import Author, Work, WorkAuthor
from book_engine.recommendations.models import (
    DerivationRun,
    TraitDefinition,
    WorkEmbedding,
    WorkRepresentation,
    WorkTraitValue,
)
from book_engine.recommendations.traits import TRAIT_EXTRACTOR_VERSION
from book_engine.recommendations.types import (
    EmbeddingBatch,
    EmbeddingProvider,
    SemanticDocument,
)

REPRESENTATION_VERSION = "semantic-document-v2"
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

    def _embed(self, text: str) -> tuple[float, ...]:
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

    def embed_many(self, texts: list[str]) -> EmbeddingBatch:
        return EmbeddingBatch(
            vectors=tuple(self._embed(text) for text in texts),
            response_metadata={"implementation": self.model},
            usage={"input_count": len(texts), "api_requests": 0, "cost_usd": 0.0},
        )


def build_semantic_document(session: Session, work_id: int) -> SemanticDocument:
    work = session.get(Work, work_id)
    if work is None:
        raise ValueError(f"Work {work_id} does not exist")
    author = session.scalar(
        select(Author.name)
        .join(WorkAuthor, WorkAuthor.author_id == Author.id)
        .where(WorkAuthor.work_id == work_id, WorkAuthor.position == 0)
    )
    traits = tuple(
        session.scalars(
            select(TraitDefinition.label)
            .join(WorkTraitValue, WorkTraitValue.trait_id == TraitDefinition.id)
            .where(
                WorkTraitValue.work_id == work_id,
                WorkTraitValue.status == "accepted",
                WorkTraitValue.extractor_version == TRAIT_EXTRACTOR_VERSION,
            )
            .distinct()
            .order_by(TraitDefinition.label)
        ).all()
    )
    snapshot: dict[str, object] = {
        "title": work.title,
        "subtitle": work.subtitle,
        "author": author or "Unknown author",
        "description": work.description,
        "recommendation_traits": traits,
        "publication_year": work.original_publication_year,
    }
    encoded = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    input_hash = hashlib.sha256(encoded.encode()).hexdigest()
    lines = [f"Title: {work.title}", f"Author: {author or 'Unknown author'}"]
    if work.subtitle and work.subtitle.casefold() not in work.title.casefold():
        lines.append(f"Subtitle: {work.subtitle}")
    if traits:
        lines.append(f"Recommendation traits: {', '.join(traits)}")
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


def ensure_embeddings(
    session: Session,
    representations: list[WorkRepresentation],
    provider: EmbeddingProvider,
) -> tuple[dict[int, WorkEmbedding], int, DerivationRun | None]:
    embeddings: dict[int, WorkEmbedding] = {}
    missing: list[WorkRepresentation] = []
    for representation in representations:
        existing = session.scalar(
            select(WorkEmbedding).where(
                WorkEmbedding.work_id == representation.work_id,
                WorkEmbedding.purpose == "recommendation_similarity",
                WorkEmbedding.provider == provider.name,
                WorkEmbedding.model == provider.model,
                WorkEmbedding.input_hash == representation.input_hash,
            )
        )
        if existing is None:
            missing.append(representation)
        else:
            embeddings[representation.work_id] = existing
    if not missing:
        return embeddings, len(representations), None

    request = {
        "purpose": "recommendation_similarity",
        "inputs": [
            {
                "work_id": item.work_id,
                "representation_id": item.id,
                "content_hash": item.input_hash,
                "builder_version": item.builder_version,
            }
            for item in missing
        ],
    }
    request_hash = _stable_hash(
        {"provider": provider.name, "model": provider.model, **request}
    )
    started = _now()
    derivation = DerivationRun(
        provider=provider.name,
        model=provider.model,
        purpose="recommendation_similarity",
        input_hash=request_hash,
        prompt_version=None,
        schema_version="embedding-batch-v1",
        request_json=request,
        usage_json={},
        status="running",
        started_at=started,
    )
    session.add(derivation)
    session.flush()
    try:
        batch = provider.embed_many([item.content for item in missing])
        if len(batch.vectors) != len(missing):
            raise ValueError("Embedding provider returned the wrong vector count")
        for representation, vector in zip(missing, batch.vectors, strict=True):
            if len(vector) != provider.dimensions:
                raise ValueError("Embedding provider returned the wrong dimensions")
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
                derivation_run_id=derivation.id,
            )
            session.add(embedding)
            embeddings[representation.work_id] = embedding
        derivation.response_json = batch.response_metadata
        derivation.usage_json = batch.usage
        derivation.status = "completed"
        derivation.completed_at = _now()
        session.flush()
    except Exception as exc:
        derivation.status = "failed"
        derivation.error_message = str(exc)
        derivation.completed_at = _now()
        session.flush()
        raise
    return embeddings, len(representations) - len(missing), derivation


def _stable_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


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
