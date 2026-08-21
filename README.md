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

After applying migrations, import a Goodreads library export with:

```bash
uv run book-engine import-goodreads data/imports/goodreads_library_export.csv
```

Build the reviewed browse facets after enrichment, then start the local library:

```bash
uv run book-engine sync-browse-facets
uv run book-engine serve
```

The server-rendered browser is available at <http://127.0.0.1:8000/library>.

Application settings use environment variables prefixed with `BOOK_ENGINE_`.
Copy `.env.example` to `.env` for local overrides. The default SQLite database is
stored at `data/book_engine.db`; local databases and personal data are ignored by
Git.
