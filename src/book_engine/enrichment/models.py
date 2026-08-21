"""Enrichment audit, provenance, and structured metadata models."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from book_engine.db import Base, TimestampMixin


class EnrichmentRun(TimestampMixin, Base):
    __tablename__ = "enrichment_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'completed', 'completed_with_errors', 'failed')",
            name="status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(100))
    mode: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="running")
    started_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]
    requested_count: Mapped[int] = mapped_column(default=0)
    succeeded_count: Mapped[int] = mapped_column(default=0)
    missed_count: Mapped[int] = mapped_column(default=0)
    ambiguous_count: Mapped[int] = mapped_column(default=0)
    failed_count: Mapped[int] = mapped_column(default=0)
    skipped_count: Mapped[int] = mapped_column(default=0)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class EnrichmentAttempt(TimestampMixin, Base):
    __tablename__ = "enrichment_attempts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'missed', 'ambiguous', 'failed', "
            "'skipped_cached')",
            name="status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("enrichment_runs.id", ondelete="CASCADE")
    )
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"))
    edition_id: Mapped[int] = mapped_column(
        ForeignKey("editions.id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(String(100))
    lookup_strategy: Mapped[str] = mapped_column(String(50))
    lookup_key: Mapped[str] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(32), default="running")
    match_method: Mapped[str | None] = mapped_column(String(50))
    match_score: Mapped[float | None]
    decision_reason: Mapped[str | None]
    candidate_summary: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error_type: Mapped[str | None] = mapped_column(String(200))
    error_message: Mapped[str | None]
    started_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]
    next_retry_at: Mapped[datetime | None]


class ProviderResponse(TimestampMixin, Base):
    __tablename__ = "provider_responses"

    id: Mapped[int] = mapped_column(primary_key=True)
    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("enrichment_attempts.id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(String(100))
    operation: Mapped[str] = mapped_column(String(32))
    request_key: Mapped[str] = mapped_column(String(1000), index=True)
    endpoint: Mapped[str] = mapped_column(String(500))
    retrieved_at: Mapped[datetime]
    status_code: Mapped[int | None]
    outcome: Mapped[str] = mapped_column(String(32))
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    payload_checksum: Mapped[str | None] = mapped_column(String(64))
    parser_version: Mapped[str] = mapped_column(String(50))
    error_message: Mapped[str | None]


class MetadataMatch(TimestampMixin, Base):
    __tablename__ = "metadata_matches"
    __table_args__ = (
        CheckConstraint(
            "status IN ('accepted', 'ambiguous', 'rejected')", name="status"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("enrichment_attempts.id", ondelete="CASCADE")
    )
    provider_response_id: Mapped[int] = mapped_column(
        ForeignKey("provider_responses.id", ondelete="CASCADE")
    )
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"))
    edition_id: Mapped[int] = mapped_column(
        ForeignKey("editions.id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(String(100))
    external_work_id: Mapped[str | None] = mapped_column(String(300))
    external_edition_id: Mapped[str | None] = mapped_column(String(300))
    match_method: Mapped[str] = mapped_column(String(50))
    match_score: Mapped[float | None]
    status: Mapped[str] = mapped_column(String(32))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON)


class MetadataClaim(TimestampMixin, Base):
    __tablename__ = "metadata_claims"
    __table_args__ = (
        CheckConstraint(
            "(work_id IS NOT NULL AND edition_id IS NULL) OR "
            "(work_id IS NULL AND edition_id IS NOT NULL)",
            name="exactly_one_owner",
        ),
        CheckConstraint(
            "source_kind IN ('goodreads_import', 'provider', 'manual')",
            name="source_kind",
        ),
        CheckConstraint(
            "status IN ('accepted', 'candidate', 'rejected', 'superseded')",
            name="status",
        ),
        Index(
            "uq_metadata_claims_accepted_work_field",
            "work_id",
            "field_name",
            unique=True,
            sqlite_where=text("status = 'accepted' AND work_id IS NOT NULL"),
        ),
        Index(
            "uq_metadata_claims_accepted_edition_field",
            "edition_id",
            "field_name",
            unique=True,
            sqlite_where=text("status = 'accepted' AND edition_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int | None] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE")
    )
    edition_id: Mapped[int | None] = mapped_column(
        ForeignKey("editions.id", ondelete="CASCADE")
    )
    field_name: Mapped[str] = mapped_column(String(100))
    value_json: Mapped[Any] = mapped_column(JSON)
    normalized_value: Mapped[str | None] = mapped_column(String(1000))
    source_kind: Mapped[str] = mapped_column(String(32))
    import_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_records.id", ondelete="SET NULL")
    )
    provider_response_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider_responses.id", ondelete="SET NULL")
    )
    provider: Mapped[str] = mapped_column(String(100))
    confidence: Mapped[float | None]
    status: Mapped[str] = mapped_column(String(32))
    selection_reason: Mapped[str | None]
    observed_at: Mapped[datetime]


class Concept(TimestampMixin, Base):
    __tablename__ = "concepts"
    __table_args__ = (UniqueConstraint("kind", "normalized_label"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    label: Mapped[str] = mapped_column(String(300))
    normalized_label: Mapped[str] = mapped_column(String(300))


class WorkConceptClaim(TimestampMixin, Base):
    __tablename__ = "work_concept_claims"
    __table_args__ = (
        UniqueConstraint("work_id", "concept_id", "provider_response_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"))
    concept_id: Mapped[int] = mapped_column(
        ForeignKey("concepts.id", ondelete="CASCADE")
    )
    provider_response_id: Mapped[int] = mapped_column(
        ForeignKey("provider_responses.id", ondelete="CASCADE")
    )
    original_label: Mapped[str] = mapped_column(String(500))
    confidence: Mapped[float | None]
    status: Mapped[str] = mapped_column(String(32), default="accepted")


class Series(TimestampMixin, Base):
    __tablename__ = "series"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    normalized_name: Mapped[str] = mapped_column(String(300), unique=True)


class WorkSeriesClaim(TimestampMixin, Base):
    __tablename__ = "work_series_claims"
    __table_args__ = (UniqueConstraint("work_id", "series_id", "provider_response_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"))
    series_id: Mapped[int] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"))
    provider_response_id: Mapped[int] = mapped_column(
        ForeignKey("provider_responses.id", ondelete="CASCADE")
    )
    sequence_label: Mapped[str | None] = mapped_column(String(100))
    confidence: Mapped[float | None]
    status: Mapped[str] = mapped_column(String(32), default="accepted")


class CoverCandidate(TimestampMixin, Base):
    __tablename__ = "cover_candidates"
    __table_args__ = (UniqueConstraint("edition_id", "provider", "external_cover_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    edition_id: Mapped[int] = mapped_column(
        ForeignKey("editions.id", ondelete="CASCADE")
    )
    provider_response_id: Mapped[int] = mapped_column(
        ForeignKey("provider_responses.id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(String(100))
    external_cover_id: Mapped[str] = mapped_column(String(300))
    small_url: Mapped[str | None] = mapped_column(String(2000))
    medium_url: Mapped[str | None] = mapped_column(String(2000))
    large_url: Mapped[str | None] = mapped_column(String(2000))
    status: Mapped[str] = mapped_column(String(32), default="candidate")
    checked_at: Mapped[datetime]
