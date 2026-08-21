# Book Engine

Local-first tools for building a canonical personal book catalog and, eventually,
explainable recommendations.

## Development

Book Engine targets Python 3.13 and uses `uv` for environment and dependency
management.

```bash
uv sync --all-groups
uv run alembic upgrade head
uv run pytest
```

Application settings use environment variables prefixed with `BOOK_ENGINE_`.
Copy `.env.example` to `.env` for local overrides. The default SQLite database is
stored at `data/book_engine.db`; local databases and personal data are ignored by
Git.
