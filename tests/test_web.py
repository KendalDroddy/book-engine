from datetime import UTC, datetime
from pathlib import Path

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import sessionmaker

from book_engine.discovery.models import DiscoveryRun
from book_engine.importing.goodreads import import_goodreads_csv
from book_engine.recommendations.models import RecommendationFeedback
from book_engine.recommendations.service import SIGNAL_WEIGHTS, run_validation
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


def test_recommendation_center_renders_local_results_and_feedback_is_idempotent(
    db_engine: Engine,
) -> None:
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    with factory() as session:
        import_goodreads_csv(session, FIXTURES / "goodreads_sample.csv")
        sync_browse_facets(session)
        run_validation(session)
        now = datetime.now(UTC).replace(tzinfo=None)
        session.add(
            DiscoveryRun(
                provider="openlibrary",
                strategy_version="fixture",
                input_hash="a" * 64,
                requested_limit=30,
                configuration={},
                status="failed",
                started_at=now,
                completed_at=now,
            )
        )
        session.commit()
    client = TestClient(create_app(factory))

    response = client.get("/recommendations")

    assert response.status_code == 200
    document = BeautifulSoup(response.text, "html.parser")
    assert "Best Matches" in document.get_text()
    assert "Want to Read Ranked" in document.get_text()
    assert "External discovery is temporarily unavailable" in document.get_text()
    card = document.select_one(".recommendation-card")
    assert card is not None
    assert card.select_one(".recommendation-why") is not None
    assert len(card.select(".signal-grid > div")) == len(SIGNAL_WEIGHTS)
    assert len(card.select(".neighbor-list span")) == 2
    action = card.select_one("form[action*='not_interested']")
    assert action is not None

    first = client.post(action["action"], follow_redirects=False)
    second = client.post(action["action"], follow_redirects=False)

    assert first.status_code == 303
    assert second.status_code == 303
    with factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(RecommendationFeedback)
                .where(RecommendationFeedback.action == "not_interested")
            )
            == 1
        )
        feedback = session.scalar(select(RecommendationFeedback))
        assert feedback is not None
        assert feedback.context_json["recommendation_run_id"] > 0
        assert feedback.context_json["rank"] == 1
