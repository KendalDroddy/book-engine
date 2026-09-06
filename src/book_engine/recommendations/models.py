"""Versioned recommendation, representation, and taste-profile models."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from book_engine.db import Base, TimestampMixin


class TraitDefinition(TimestampMixin, Base):
    __tablename__ = "trait_definitions"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    label: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(50))
    value_type: Mapped[str] = mapped_column(String(32), default="boolean")
    description: Mapped[str | None]
    schema_version: Mapped[str] = mapped_column(String(50))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class DerivationRun(TimestampMixin, Base):
    __tablename__ = "derivation_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running', 'completed', 'failed')", name="status"),
        UniqueConstraint("provider", "model", "purpose", "input_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(200))
    purpose: Mapped[str] = mapped_column(String(100))
    input_hash: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    schema_version: Mapped[str] = mapped_column(String(50))
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    usage_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32))
    error_message: Mapped[str | None]
    started_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]


class WorkTraitValue(TimestampMixin, Base):
    __tablename__ = "work_trait_values"
    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('deterministic', 'provider', 'ai', 'manual')",
            name="source_kind",
        ),
        UniqueConstraint(
            "work_id", "trait_id", "source_kind", "extractor_version", "input_hash"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"))
    trait_id: Mapped[int] = mapped_column(
        ForeignKey("trait_definitions.id", ondelete="CASCADE")
    )
    value_text: Mapped[str | None] = mapped_column(String(500))
    value_number: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    source_kind: Mapped[str] = mapped_column(String(32))
    source_reference: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    derivation_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("derivation_runs.id", ondelete="SET NULL")
    )
    extractor_version: Mapped[str] = mapped_column(String(50))
    input_hash: Mapped[str] = mapped_column(String(64))
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="accepted")


class WorkRepresentation(TimestampMixin, Base):
    __tablename__ = "work_representations"
    __table_args__ = (
        UniqueConstraint("work_id", "kind", "builder_version", "input_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(50))
    content: Mapped[str]
    input_hash: Mapped[str] = mapped_column(String(64))
    builder_version: Mapped[str] = mapped_column(String(50))
    source_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    derivation_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("derivation_runs.id", ondelete="SET NULL")
    )


class WorkEmbedding(TimestampMixin, Base):
    __tablename__ = "work_embeddings"
    __table_args__ = (
        UniqueConstraint("work_id", "purpose", "provider", "model", "input_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"))
    representation_id: Mapped[int] = mapped_column(
        ForeignKey("work_representations.id", ondelete="CASCADE")
    )
    purpose: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(200))
    dimensions: Mapped[int]
    vector_blob: Mapped[bytes] = mapped_column(LargeBinary)
    input_hash: Mapped[str] = mapped_column(String(64))
    representation_version: Mapped[str] = mapped_column(String(50))
    derivation_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("derivation_runs.id", ondelete="SET NULL")
    )


class TasteProfileRun(TimestampMixin, Base):
    __tablename__ = "taste_profile_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running', 'completed', 'failed')", name="status"),
        UniqueConstraint("algorithm_version", "input_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    algorithm_version: Mapped[str] = mapped_column(String(50))
    input_hash: Mapped[str] = mapped_column(String(64))
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON)
    source_work_count: Mapped[int]
    represented_work_count: Mapped[int]
    centroid_blob: Mapped[bytes | None] = mapped_column(LargeBinary)
    dimensions: Mapped[int | None]
    status: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]


class TasteProfileValue(Base):
    __tablename__ = "taste_profile_values"

    profile_run_id: Mapped[int] = mapped_column(
        ForeignKey("taste_profile_runs.id", ondelete="CASCADE"), primary_key=True
    )
    signal_kind: Mapped[str] = mapped_column(String(50), primary_key=True)
    signal_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    label: Mapped[str] = mapped_column(String(200))
    weight: Mapped[float] = mapped_column(Float)
    support_count: Mapped[int]
    confidence: Mapped[float] = mapped_column(Float)
    representative_work_ids: Mapped[list[int]] = mapped_column(JSON, default=list)


class RecommendationRun(TimestampMixin, Base):
    __tablename__ = "recommendation_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running', 'completed', 'failed')", name="status"),
        UniqueConstraint("algorithm_version", "input_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_run_id: Mapped[int] = mapped_column(
        ForeignKey("taste_profile_runs.id", ondelete="CASCADE")
    )
    algorithm_version: Mapped[str] = mapped_column(String(50))
    input_hash: Mapped[str] = mapped_column(String(64))
    candidate_source: Mapped[str] = mapped_column(String(100))
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]


class RecommendationItem(TimestampMixin, Base):
    __tablename__ = "recommendation_items"
    __table_args__ = (UniqueConstraint("run_id", "work_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("recommendation_runs.id", ondelete="CASCADE")
    )
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"))
    rank: Mapped[int]
    base_score: Mapped[float] = mapped_column(Float)
    reranked_score: Mapped[float] = mapped_column(Float)
    match_label: Mapped[str] = mapped_column(String(50))
    confidence_score: Mapped[float] = mapped_column(Float)
    confidence_label: Mapped[str] = mapped_column(String(20))
    repetitive: Mapped[bool] = mapped_column(Boolean, default=False)
    display_eligible: Mapped[bool] = mapped_column(Boolean, default=True)
    eligible_rank: Mapped[int | None]
    eligibility_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    eligibility_warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    eligibility_provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class RecommendationSignal(Base):
    __tablename__ = "recommendation_signals"
    __table_args__ = (UniqueConstraint("recommendation_item_id", "signal_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    recommendation_item_id: Mapped[int] = mapped_column(
        ForeignKey("recommendation_items.id", ondelete="CASCADE")
    )
    signal_name: Mapped[str] = mapped_column(String(100))
    raw_value: Mapped[float] = mapped_column(Float)
    normalized_value: Mapped[float] = mapped_column(Float)
    weight: Mapped[float] = mapped_column(Float)
    contribution: Mapped[float] = mapped_column(Float)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    algorithm_version: Mapped[str] = mapped_column(String(50))


class RecommendationNeighbor(Base):
    __tablename__ = "recommendation_neighbors"

    recommendation_item_id: Mapped[int] = mapped_column(
        ForeignKey("recommendation_items.id", ondelete="CASCADE"), primary_key=True
    )
    read_work_id: Mapped[int] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), primary_key=True
    )
    rank: Mapped[int]
    similarity: Mapped[float] = mapped_column(Float)
    relationship_type: Mapped[str] = mapped_column(String(50))


class RecommendationExplanation(Base):
    __tablename__ = "recommendation_explanations"

    id: Mapped[int] = mapped_column(primary_key=True)
    recommendation_item_id: Mapped[int] = mapped_column(
        ForeignKey("recommendation_items.id", ondelete="CASCADE"), unique=True
    )
    explanation_type: Mapped[str] = mapped_column(String(50))
    structured_evidence: Mapped[dict[str, Any]] = mapped_column(JSON)
    rendered_text: Mapped[str]
    generator: Mapped[str] = mapped_column(String(100))
    generator_version: Mapped[str] = mapped_column(String(50))


class RecommendationFeedback(TimestampMixin, Base):
    __tablename__ = "recommendation_feedback"
    __table_args__ = (
        CheckConstraint(
            "action IN ('add_to_want_to_read', 'not_interested', "
            "'loved', 'liked', 'fine', 'miss')",
            name="action",
        ),
        UniqueConstraint("work_id", "action"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"))
    recommendation_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("recommendation_items.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(50))
    source: Mapped[str] = mapped_column(String(100))
    context_json: Mapped[dict[str, Any]] = mapped_column(JSON)
