from book_engine.config import Settings


def test_default_database_is_local_sqlite() -> None:
    settings = Settings()

    assert settings.database_url == "sqlite:///./data/book_engine.db"
    assert settings.sql_echo is False
