"""Database engine, session, and shared model definitions."""

from collections.abc import Iterator
from datetime import datetime

from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.sql import func

from book_engine.config import get_settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


def create_db_engine(
    database_url: str | None = None, *, echo: bool | None = None
) -> Engine:
    settings = get_settings()
    engine = create_engine(
        database_url or settings.database_url,
        echo=settings.sql_echo if echo is None else echo,
    )

    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _configure_sqlite)

    return engine


def _configure_sqlite(dbapi_connection: object, _connection_record: object) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


engine = create_db_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def session_scope() -> Iterator[Session]:
    with SessionLocal.begin() as session:
        yield session
