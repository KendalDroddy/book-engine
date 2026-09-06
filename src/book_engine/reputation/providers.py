"""Automated reputation providers."""

from typing import Any, cast

import httpx

from book_engine.reputation.types import (
    ReputationCandidate,
    ReputationLookup,
    ReputationProviderError,
    ReputationProviderResult,
)

BASE_URL = "https://www.googleapis.com/books/v1"


class GoogleBooksReputationProvider:
    name = "google_books"
    parser_version = "google-books-reputation-v1"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 15.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._client = client or httpx.Client(
            base_url=BASE_URL,
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"Accept": "application/json", "User-Agent": "BookEngine/0.1"},
        )

    def lookup(self, lookup: ReputationLookup) -> ReputationProviderResult:
        responses: list[dict[str, Any]] = []
        candidates: list[ReputationCandidate] = []
        status_code = 200
        queries = [f"isbn:{lookup.isbns[0]}"] if lookup.isbns else []
        queries.append(f'intitle:"{lookup.title}" inauthor:"{lookup.author}"')
        for query in queries:
            payload, status_code = self._get(query)
            responses.append({"query": query, "payload": payload})
            candidates.extend(parse_google_books_payload(payload))
            if query.startswith("isbn:") and any(
                candidate.average_rating is not None
                and bool(set(candidate.identifiers) & set(lookup.isbns))
                for candidate in candidates
            ):
                break
        return ReputationProviderResult(
            request_key=f"work:{lookup.work_id}",
            endpoint="/volumes",
            status_code=status_code,
            raw_response={"responses": responses},
            candidates=tuple(
                {item.provider_book_id: item for item in candidates}.values()
            ),
            request_count=len(responses),
        )

    def _get(self, query: str) -> tuple[dict[str, Any], int]:
        params: dict[str, str | int] = {
            "q": query,
            "maxResults": 10,
            "printType": "books",
            "projection": "full",
        }
        if self._api_key:
            params["key"] = self._api_key
        try:
            response = self._client.get("/volumes", params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ReputationProviderError(str(exc)) from exc
        if not isinstance(payload, dict):
            raise ReputationProviderError("Google Books returned a non-object response")
        return payload, response.status_code


def parse_google_books_payload(
    payload: dict[str, Any],
) -> tuple[ReputationCandidate, ...]:
    items = payload.get("items")
    if not isinstance(items, list):
        return ()
    parsed: list[ReputationCandidate] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        info = item.get("volumeInfo")
        if not isinstance(info, dict) or not isinstance(info.get("title"), str):
            continue
        authors = cast(
            list[Any],
            info.get("authors") if isinstance(info.get("authors"), list) else [],
        )
        identifiers = cast(
            list[Any],
            info.get("industryIdentifiers")
            if isinstance(info.get("industryIdentifiers"), list)
            else [],
        )
        parsed.append(
            ReputationCandidate(
                provider_book_id=item["id"],
                title=info["title"],
                authors=tuple(value for value in authors if isinstance(value, str)),
                identifiers=tuple(
                    value
                    for entry in identifiers
                    if isinstance(entry, dict)
                    if isinstance((value := entry.get("identifier")), str)
                ),
                average_rating=_rating(info.get("averageRating")),
                ratings_count=_count(info.get("ratingsCount")),
            )
        )
    return tuple(parsed)


def _rating(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        rating = float(value)
        return rating if 1 <= rating <= 5 else None
    return None


def _count(value: object) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )
