from pathlib import Path

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from book_engine.importing.goodreads import import_goodreads_csv
from book_engine.web.app import create_app
from book_engine.web.facets import sync_browse_facets

FIXTURES = Path(__file__).parent / "fixtures"


def _client(db_engine: Engine) -> TestClient:
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    with factory() as session:
        import_goodreads_csv(session, FIXTURES / "goodreads_sample.csv")
        sync_browse_facets(session)
    return TestClient(create_app(factory))


def test_library_search_filter_sort_and_fallback_cover(db_engine: Engine) -> None:
    client = _client(db_engine)

    response = client.get(
        "/library",
        params={
            "q": "repeat author",
            "status": "read",
            "sort": "title",
            "view": "list",
        },
    )

    assert response.status_code == 200
    document = BeautifulSoup(response.text, "html.parser")
    cards = document.select(".book-card")
    assert len(cards) == 1
    assert "A Reread Book" in cards[0].get_text()
    assert document.select_one(".book-collection--list") is not None
    assert document.select_one(".cover-fallback") is not None
    assert document.select_one(".cover img") is None
    selected_status = document.select_one("select[name='status'] option[selected]")
    assert selected_status is not None
    assert selected_status["value"] == "read"


def test_library_clamps_page_and_handles_empty_results(db_engine: Engine) -> None:
    client = _client(db_engine)

    clamped = client.get("/library", params={"page": 99})
    empty = client.get("/library", params={"q": "definitely absent"})

    assert clamped.status_code == 200
    document = BeautifulSoup(clamped.text, "html.parser")
    toolbar = document.select_one(".result-toolbar")
    assert toolbar is not None
    assert "3 results" in toolbar.get_text(" ", strip=True)
    assert empty.status_code == 200
    assert "No books found" in empty.text


def test_book_detail_and_provenance_are_separate(db_engine: Engine) -> None:
    client = _client(db_engine)

    detail = client.get("/books/1")
    provenance = client.get("/books/1/provenance")

    assert detail.status_code == 200
    assert "A Read Book" in detail.text
    assert "My reading record" in detail.text
    assert "Finished August 18, 2026" in detail.text
    assert "Inspect metadata provenance" in detail.text
    assert "Field claims" not in detail.text
    assert provenance.status_code == 200
    assert "Field claims" in provenance.text
    assert "goodreads" in provenance.text


def test_health_redirect_and_missing_book(db_engine: Engine) -> None:
    client = _client(db_engine)

    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/", follow_redirects=False).headers["location"] == "/library"
    assert client.get("/books/9999").status_code == 404
