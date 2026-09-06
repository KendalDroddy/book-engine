"""Book reputation observations and preserved provider responses."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Float,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from book_engine.db import Base, TimestampMixin


class ReputationFetch(TimestampMixin, Base):
    __tablename__ = "reputation_fetches"
    __table_args__ = (
        CheckConstraint(
            "status IN ('succeeded', 'missed', 'ambiguous', 'failed')",
            name="status",
        ),
        UniqueConstraint("provider", "input_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(100))
    request_key: Mapped[str] = mapped_column(String(1000))
    input_hash: Mapped[str] = mapped_column(String(64))
    endpoint: Mapped[str] = mapped_column(String(500))
    fetched_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(32))
    match_method: Mapped[str | None] = mapped_column(String(100))
    match_confidence: Mapped[float | None] = mapped_column(Float)
    provider_book_id: Mapped[str | None] = mapped_column(String(300))
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    decision_provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None]


class ReputationObservation(TimestampMixin, Base):
    __tablename__ = "reputation_observations"
    __table_args__ = (
        UniqueConstraint("fetch_id"),
        UniqueConstraint("provider", "provider_book_id", "model_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    fetch_id: Mapped[int] = mapped_column(
        ForeignKey("reputation_fetches.id", ondelete="CASCADE")
    )
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(100))
    provider_book_id: Mapped[str] = mapped_column(String(300))
    average_rating: Mapped[float] = mapped_column(Float)
    ratings_count: Mapped[int]
    normalized_score: Mapped[float] = mapped_column(Float)
    reputation_confidence: Mapped[float] = mapped_column(Float)
    label: Mapped[str] = mapped_column(String(100))
    model_version: Mapped[str] = mapped_column(String(100))
    model_provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    fetched_at: Mapped[datetime]
