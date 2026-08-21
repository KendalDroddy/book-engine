"""Import audit database models."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from book_engine.db import Base, TimestampMixin


class ImportRun(TimestampMixin, Base):
    __tablename__ = "import_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'completed_with_errors', "
            "'failed')",
            name="status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_type: Mapped[str] = mapped_column(String(50))
    source_filename: Mapped[str] = mapped_column(String(500))
    file_checksum: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String(32), default="pending")
    total_rows: Mapped[int] = mapped_column(default=0)
    created_rows: Mapped[int] = mapped_column(default=0)
    updated_rows: Mapped[int] = mapped_column(default=0)
    unchanged_rows: Mapped[int] = mapped_column(default=0)
    ambiguous_rows: Mapped[int] = mapped_column(default=0)
    failed_rows: Mapped[int] = mapped_column(default=0)
    error_summary: Mapped[str | None]

    records: Mapped[list["ImportRecord"]] = relationship(
        back_populates="import_run", cascade="all, delete-orphan"
    )


class ImportRecord(TimestampMixin, Base):
    __tablename__ = "import_records"
    __table_args__ = (
        CheckConstraint(
            "resolution_status IN ('pending', 'created', 'matched', 'updated', "
            "'unchanged', 'ambiguous', 'failed')",
            name="resolution_status",
        ),
        UniqueConstraint("import_run_id", "source_row_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    import_run_id: Mapped[int] = mapped_column(
        ForeignKey("import_runs.id", ondelete="CASCADE")
    )
    source_row_number: Mapped[int]
    source_record_key: Mapped[str | None] = mapped_column(String(300))
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    raw_payload_checksum: Mapped[str] = mapped_column(String(64))
    work_id: Mapped[int | None] = mapped_column(
        ForeignKey("works.id", ondelete="SET NULL")
    )
    edition_id: Mapped[int | None] = mapped_column(
        ForeignKey("editions.id", ondelete="SET NULL")
    )
    resolution_status: Mapped[str] = mapped_column(String(32), default="pending")
    resolution_notes: Mapped[str | None]

    import_run: Mapped[ImportRun] = relationship(back_populates="records")
