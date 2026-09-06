"""Candidate discovery provenance models."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from book_engine.db import Base, TimestampMixin


class DiscoveryRun(TimestampMixin, Base):
    __tablename__ = "discovery_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'completed', 'completed_with_errors', 'failed')",
            name="status",
        ),
        UniqueConstraint("provider", "strategy_version", "input_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(100))
    strategy_version: Mapped[str] = mapped_column(String(50))
    input_hash: Mapped[str] = mapped_column(String(64))
    requested_limit: Mapped[int]
    selected_count: Mapped[int] = mapped_column(default=0)
    excluded_count: Mapped[int] = mapped_column(default=0)
    external_request_count: Mapped[int] = mapped_column(default=0)
    cache_hit: Mapped[bool] = mapped_column(default=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]


class DiscoveryQuery(TimestampMixin, Base):
    __tablename__ = "discovery_queries"
    __table_args__ = (UniqueConstraint("run_id", "cluster_slug"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("discovery_runs.id", ondelete="CASCADE")
    )
    cluster_slug: Mapped[str] = mapped_column(String(100))
    query_text: Mapped[str] = mapped_column(String(1000))
    request_key: Mapped[str] = mapped_column(String(1000))
    endpoint: Mapped[str] = mapped_column(String(500))
    retrieved_at: Mapped[datetime]
    status_code: Mapped[int | None]
    outcome: Mapped[str] = mapped_column(String(32))
    result_count: Mapped[int] = mapped_column(default=0)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    payload_checksum: Mapped[str | None] = mapped_column(String(64))
    parser_version: Mapped[str] = mapped_column(String(50))
    error_message: Mapped[str | None]


class DiscoveryCandidate(TimestampMixin, Base):
    __tablename__ = "discovery_candidates"
    __table_args__ = (
        CheckConstraint(
            "status IN ('selected', 'enriched', 'enrichment_failed')", name="status"
        ),
        UniqueConstraint(
            "run_id", "work_id", name="uq_discovery_candidates_run_id_work_id"
        ),
        UniqueConstraint(
            "run_id",
            "provider",
            "external_work_id",
            name="uq_discovery_candidates_run_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("discovery_runs.id", ondelete="CASCADE")
    )
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(100))
    external_work_id: Mapped[str] = mapped_column(String(300))
    title: Mapped[str] = mapped_column(String(500))
    primary_author: Mapped[str] = mapped_column(String(300))
    discovery_rank: Mapped[int]
    cluster_slugs: Mapped[list[str]] = mapped_column(JSON)
    query_ids: Mapped[list[int]] = mapped_column(JSON)
    identifiers: Mapped[dict[str, Any]] = mapped_column(JSON)
    source_metadata: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32))
    enrichment_attempt_id: Mapped[int | None] = mapped_column(
        ForeignKey("enrichment_attempts.id", ondelete="SET NULL")
    )
    error_message: Mapped[str | None]
