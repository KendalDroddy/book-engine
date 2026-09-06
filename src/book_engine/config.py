"""Application configuration."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded from environment variables and an optional `.env` file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="BOOK_ENGINE_",
        extra="ignore",
    )

    database_url: str = "sqlite:///./data/book_engine.db"
    sql_echo: bool = False
    openlibrary_contact_email: str | None = None
    openlibrary_timeout_seconds: float = 15.0
    google_books_api_key: str | None = None
    google_books_timeout_seconds: float = 15.0
    openai_api_key: str | None = None
    openai_embedding_model: str = "text-embedding-3-small"
    openai_embedding_dimensions: int = 1536
    openai_timeout_seconds: float = 30.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
