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


@lru_cache
def get_settings() -> Settings:
    return Settings()
