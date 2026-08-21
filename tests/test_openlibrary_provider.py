import json
from pathlib import Path
from typing import Any

import httpx

from book_engine.enrichment.providers.openlibrary import OpenLibraryProvider
from book_engine.enrichment.types import BookLookup

FIXTURES = Path(__file__).parent / "fixtures" / "openlibrary"


def _fixture(name: str) -> dict[str, Any]:
    loaded: object = json.loads((FIXTURES / name).read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_openlibrary_parses_recorded_search_and_detail_payloads() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search.json":
            return httpx.Response(200, json=_fixture("search_isbn.json"))
        if request.url.path == "/isbn/9780123456786.json":
            return httpx.Response(200, json=_fixture("edition.json"))
        if request.url.path == "/works/OL1000W.json":
            return httpx.Response(200, json=_fixture("work.json"))
        raise AssertionError(f"Unexpected request: {request.url}")

    provider = OpenLibraryProvider(
        client=httpx.Client(
            transport=httpx.MockTransport(handler), base_url="https://openlibrary.org"
        ),
        minimum_interval=0,
    )
    lookup = BookLookup(
        work_id=1,
        edition_id=1,
        title="A Read Book",
        primary_author="Primary Author",
        publication_year=2020,
        isbn10="0123456789",
        isbn13="9780123456786",
    )

    search = provider.search(lookup)
    assert search.raw_payload == _fixture("search_isbn.json")
    assert len(search.candidates) == 1
    assert search.candidates[0].external_work_id == "OL1000W"
    assert search.candidates[0].matched_identifier == (
        "isbn13",
        "9780123456786",
    )

    result = provider.fetch(search.candidates[0])
    assert result.raw_payload["work"] == _fixture("work.json")
    assert result.raw_payload["edition"] == _fixture("edition.json")
    assert result.metadata.external_edition_id == "OL2000M"
    assert result.metadata.description == "A recorded description."
    assert result.metadata.publisher == "Provider Press"
    assert result.metadata.publication_date is not None
    assert result.metadata.publication_date.isoformat() == "2020-10-01"
    assert result.metadata.subjects == ("Example subject", "Structured subject")
    assert result.metadata.series == ("Example Series, #1",)
    assert result.metadata.cover_id == "123456"
