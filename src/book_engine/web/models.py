"""Persistent browse taxonomy models."""

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from book_engine.db import Base, TimestampMixin


class BrowseFacet(TimestampMixin, Base):
    __tablename__ = "browse_facets"
    __table_args__ = (UniqueConstraint("category", "label"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    label: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(50))
    display_order: Mapped[int] = mapped_column(default=0)


class ConceptFacetMapping(TimestampMixin, Base):
    __tablename__ = "concept_facet_mappings"

    concept_id: Mapped[int] = mapped_column(
        ForeignKey("concepts.id", ondelete="CASCADE"), primary_key=True
    )
    facet_id: Mapped[int] = mapped_column(
        ForeignKey("browse_facets.id", ondelete="CASCADE"), primary_key=True
    )
    mapping_rule: Mapped[str] = mapped_column(String(100), default="curated_alias")
