from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from book_engine.catalog.models import Work
from book_engine.reputation.models import ReputationFetch
from book_engine.reputation.providers import (
    GoogleBooksReputationProvider,
    parse_google_books_payload,
)
from book_engine.reputation.service import _observation, _select_candidate
from book_engine.reputation.types import ReputationCandidate, ReputationLookup


def test_google_books_parser_uses_recorded_payload() -> None:
    payload = {
        "items": [
            {
                "id": "volume-1",
                "volumeInfo": {
                    "title": "Einstein",
                    "authors": ["Walter Isaacson"],
                    "industryIdentifiers": [
                        {"type": "ISBN_13", "identifier": "9780743264747"}
                    ],
                    "averageRating": 4.2,
                    "ratingsCount": 205000,
                },
            }
        ]
    }

    parsed = parse_google_books_payload(payload)

    assert parsed[0].provider_book_id == "volume-1"
    assert parsed[0].average_rating == 4.2
    assert parsed[0].ratings_count == 205000


def test_google_books_provider_prefers_isbn_and_does_not_require_api_key() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "exact",
                        "volumeInfo": {
                            "title": "Example",
                            "authors": ["Writer"],
                            "industryIdentifiers": [
                                {"type": "ISBN_13", "identifier": "123"}
                            ],
                            "averageRating": 4.1,
                            "ratingsCount": 5000,
                        },
                    }
                ]
            },
        )

    provider = GoogleBooksReputationProvider(
        client=httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://www.googleapis.com/books/v1",
        )
    )
    result = provider.lookup(ReputationLookup(1, "Example", "Writer", ("123",)))

    assert result.request_count == 1
    assert requests[0].url.params["q"] == "isbn:123"
    assert "key" not in requests[0].url.params


def test_matching_and_bayesian_quality_are_confidence_aware(
    db_session: Session,
) -> None:
    lookup = ReputationLookup(1, "Example", "Writer", ("9780000000001",))
    highly_rated_tiny = ReputationCandidate(
        "tiny",
        "Example",
        ("Writer",),
        ("9780000000001",),
        4.6,
        10,
    )
    established = ReputationCandidate(
        "established",
        "Example",
        ("Writer",),
        ("9780000000001",),
        4.2,
        50_000,
    )
    status, selected, method, confidence, _ = _select_candidate(
        lookup, (highly_rated_tiny, established)
    )
    assert (status, selected, method, confidence) == (
        "succeeded",
        established,
        "isbn",
        1.0,
    )

    work = Work(title="Example", fiction_status="unknown")
    db_session.add(work)
    db_session.flush()
    fetch = ReputationFetch(
        work_id=work.id,
        provider="fixture",
        request_key="fixture",
        input_hash="x" * 64,
        endpoint="fixture",
        fetched_at=datetime(2026, 9, 5),
        status="succeeded",
        match_method="isbn",
        match_confidence=1.0,
        provider_book_id="established",
        raw_response={},
        decision_provenance={},
    )
    db_session.add(fetch)
    db_session.flush()
    tiny_observation = _observation(fetch, highly_rated_tiny)
    established_observation = _observation(fetch, established)
    negative_observation = _observation(
        fetch,
        ReputationCandidate(
            "negative",
            "Example",
            ("Writer",),
            ("9780000000001",),
            3.2,
            50_000,
        ),
    )

    assert tiny_observation.reputation_confidence < 0.02
    assert established_observation.reputation_confidence > 0.9
    assert established_observation.normalized_score > tiny_observation.normalized_score
    assert negative_observation.normalized_score < 50
    assert negative_observation.reputation_confidence > 0.9
