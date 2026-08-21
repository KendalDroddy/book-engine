"""Personal library database models."""

from datetime import date

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from book_engine.db import Base, TimestampMixin


class LibraryEntry(TimestampMixin, Base):
    __tablename__ = "library_entries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('read', 'reading', 'want_to_read', "
            "'saved_recommendation', 'rejected')",
            name="status",
        ),
        CheckConstraint(
            "personal_rating IS NULL OR personal_rating BETWEEN 1 AND 5",
            name="personal_rating",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), unique=True
    )
    status: Mapped[str] = mapped_column(String(32))
    personal_rating: Mapped[int | None]
    personal_notes: Mapped[str | None]
    first_read_on: Mapped[date | None]
    last_read_on: Mapped[date | None]

    reading_events: Mapped[list["ReadingEvent"]] = relationship(
        back_populates="library_entry", cascade="all, delete-orphan"
    )
    shelf_links: Mapped[list["LibraryEntryShelf"]] = relationship(
        back_populates="library_entry", cascade="all, delete-orphan"
    )


class ReadingEvent(TimestampMixin, Base):
    __tablename__ = "reading_events"
    __table_args__ = (
        CheckConstraint(
            "date_precision IN ('day', 'month', 'year', 'unknown')",
            name="date_precision",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    library_entry_id: Mapped[int] = mapped_column(
        ForeignKey("library_entries.id", ondelete="CASCADE")
    )
    started_on: Mapped[date | None]
    finished_on: Mapped[date | None]
    date_precision: Mapped[str] = mapped_column(String(16), default="unknown")
    source: Mapped[str] = mapped_column(String(100))

    library_entry: Mapped[LibraryEntry] = relationship(back_populates="reading_events")


class Shelf(TimestampMixin, Base):
    __tablename__ = "shelves"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)

    library_entry_links: Mapped[list["LibraryEntryShelf"]] = relationship(
        back_populates="shelf"
    )


class LibraryEntryShelf(Base):
    __tablename__ = "library_entry_shelves"

    library_entry_id: Mapped[int] = mapped_column(
        ForeignKey("library_entries.id", ondelete="CASCADE"), primary_key=True
    )
    shelf_id: Mapped[int] = mapped_column(
        ForeignKey("shelves.id", ondelete="CASCADE"), primary_key=True
    )

    library_entry: Mapped[LibraryEntry] = relationship(back_populates="shelf_links")
    shelf: Mapped[Shelf] = relationship(back_populates="library_entry_links")
