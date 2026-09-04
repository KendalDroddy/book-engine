"""Provider-neutral semantic derivation contracts."""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class SemanticDocument:
    work_id: int
    content: str
    input_hash: str
    source_snapshot: dict[str, object]


class EmbeddingProvider(Protocol):
    name: str
    model: str
    dimensions: int

    def embed_many(self, texts: list[str]) -> "EmbeddingBatch": ...


@dataclass(frozen=True)
class EmbeddingBatch:
    vectors: tuple[tuple[float, ...], ...]
    response_metadata: dict[str, Any]
    usage: dict[str, Any]


@dataclass(frozen=True)
class TraitAssertion:
    trait_slug: str
    value_text: str | None
    value_number: float | None
    confidence: float
    evidence: dict[str, object]


class TraitProvider(Protocol):
    name: str
    model: str
    schema_version: str

    def extract(self, document: SemanticDocument) -> tuple[TraitAssertion, ...]: ...
