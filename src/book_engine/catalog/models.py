"""Canonical catalog database models."""

from datetime import date

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from book_engine.db import Base, TimestampMixin


class Work(TimestampMixin, Base):
    __tablename__ = "works"
    __table_args__ = (
        CheckConstraint(
            "fiction_status IN ('fiction', 'nonfiction', 'mixed', 'unknown')",
            name="fiction_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    subtitle: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None]
    original_publication_year: Mapped[int | None]
    language: Mapped[str | None] = mapped_column(String(32))
    fiction_status: Mapped[str] = mapped_column(String(16), default="unknown")

    editions: Mapped[list["Edition"]] = relationship(
        back_populates="work", cascade="all, delete-orphan"
    )
    author_links: Mapped[list["WorkAuthor"]] = relationship(
        back_populates="work", cascade="all, delete-orphan"
    )
    identifiers: Mapped[list["Identifier"]] = relationship(back_populates="work")


class Edition(TimestampMixin, Base):
    __tablename__ = "editions"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"))
    title: Mapped[str | None] = mapped_column(String(500))
    format: Mapped[str | None] = mapped_column(String(100))
    publisher: Mapped[str | None] = mapped_column(String(300))
    publication_date: Mapped[date | None]
    publication_year: Mapped[int | None]
    page_count: Mapped[int | None] = mapped_column(Integer)
    language: Mapped[str | None] = mapped_column(String(32))
    cover_url: Mapped[str | None] = mapped_column(String(2000))
    cover_source: Mapped[str | None] = mapped_column(String(100))

    work: Mapped[Work] = relationship(back_populates="editions")
    identifiers: Mapped[list["Identifier"]] = relationship(back_populates="edition")


class Author(TimestampMixin, Base):
    __tablename__ = "authors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    sort_name: Mapped[str | None] = mapped_column(String(300), index=True)
    external_key: Mapped[str | None] = mapped_column(String(300), unique=True)

    work_links: Mapped[list["WorkAuthor"]] = relationship(back_populates="author")


class WorkAuthor(Base):
    __tablename__ = "work_authors"

    work_id: Mapped[int] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), primary_key=True
    )
    author_id: Mapped[int] = mapped_column(
        ForeignKey("authors.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(50), primary_key=True, default="author")
    position: Mapped[int] = mapped_column(default=0)

    work: Mapped[Work] = relationship(back_populates="author_links")
    author: Mapped[Author] = relationship(back_populates="work_links")


class Identifier(TimestampMixin, Base):
    __tablename__ = "identifiers"
    __table_args__ = (
        CheckConstraint(
            "(work_id IS NOT NULL AND edition_id IS NULL) OR "
            "(work_id IS NULL AND edition_id IS NOT NULL)",
            name="exactly_one_owner",
        ),
        UniqueConstraint("scheme", "value"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int | None] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE")
    )
    edition_id: Mapped[int | None] = mapped_column(
        ForeignKey("editions.id", ondelete="CASCADE")
    )
    scheme: Mapped[str] = mapped_column(String(50))
    value: Mapped[str] = mapped_column(String(300))
    source: Mapped[str] = mapped_column(String(100))

    work: Mapped[Work | None] = relationship(back_populates="identifiers")
    edition: Mapped[Edition | None] = relationship(back_populates="identifiers")
