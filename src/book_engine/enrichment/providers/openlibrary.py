"""Open Library metadata provider."""

import re
import time
from datetime import date
from typing import Any

import httpx

from book_engine.enrichment.types import (
    BookLookup,
    BookMetadata,
    MetadataCandidate,
    ProviderError,
    ProviderMetadataResult,
    ProviderSearchResult,
)

BASE_URL = "https://openlibrary.org"
SEARCH_FIELDS = ",".join(
    (
        "key",
        "title",
        "author_name",
        "first_publish_year",
        "isbn",
        "cover_i",
    )
)
PARSER_VERSION = "openlibrary-v1"


class OpenLibraryProvider:
    name = "openlibrary"

    def __init__(
        self,
        *,
        contact_email: str | None = None,
        timeout_seconds: float = 15.0,
        client: httpx.Client | None = None,
        minimum_interval: float | None = None,
    ) -> None:
        identity = "BookEngine/0.1"
        if contact_email:
            identity = f"{identity} ({contact_email})"
        self._client = client or httpx.Client(
            base_url=BASE_URL,
            headers={"User-Agent": identity, "Accept": "application/json"},
            timeout=timeout_seconds,
            follow_redirects=True,
        )
        self._minimum_interval = (
            minimum_interval
            if minimum_interval is not None
            else (1 / 3 if contact_email else 1.0)
        )
        self._last_request_at: float | None = None

    def search(self, lookup: BookLookup) -> ProviderSearchResult:
        isbn = lookup.isbn13 or lookup.isbn10
        if isbn:
            params: dict[str, str | int] = {
                "q": f"isbn:{isbn}",
                "fields": SEARCH_FIELDS,
                "limit": 5,
            }
        else:
            params = {
                "title": _lookup_title(lookup.title),
                "author": lookup.primary_author,
                "fields": SEARCH_FIELDS,
                "limit": 5,
            }

        payload, status_code = self._get_json(
            "/search.json", params=params, request_key=lookup.request_key
        )
        return ProviderSearchResult(
            request_key=lookup.request_key,
            endpoint="/search.json",
            status_code=status_code,
            raw_payload=payload,
            candidates=parse_search_payload(payload, lookup),
        )

    def fetch(self, candidate: MetadataCandidate) -> ProviderMetadataResult:
        request_key = f"work:{candidate.external_work_id}"
        raw_payload: dict[str, Any] = {}
        edition_payload: dict[str, Any] | None = None

        if candidate.matched_identifier is not None:
            _, isbn = candidate.matched_identifier
            edition_payload, _ = self._get_json(
                f"/isbn/{isbn}.json",
                request_key=f"isbn:{isbn}",
            )
            raw_payload["edition"] = edition_payload

        work_path = f"/works/{candidate.external_work_id}.json"
        work_payload, status_code = self._get_json(work_path, request_key=request_key)
        raw_payload["work"] = work_payload

        return ProviderMetadataResult(
            request_key=request_key,
            endpoint=work_path,
            status_code=status_code,
            raw_payload=raw_payload,
            metadata=parse_metadata_payload(candidate, work_payload, edition_payload),
        )

    def _get_json(
        self,
        path: str,
        *,
        request_key: str,
        params: dict[str, str | int] | None = None,
    ) -> tuple[dict[str, Any], int]:
        self._throttle()
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise ProviderError(
                str(exc), request_key=request_key, endpoint=path
            ) from exc
        self._last_request_at = time.monotonic()

        if response.status_code != 200:
            raise ProviderError(
                f"Open Library returned HTTP {response.status_code}",
                request_key=request_key,
                endpoint=path,
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(
                "Open Library returned invalid JSON",
                request_key=request_key,
                endpoint=path,
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError(
                "Open Library returned a non-object JSON response",
                request_key=request_key,
                endpoint=path,
                status_code=response.status_code,
            )
        return payload, response.status_code

    def _throttle(self) -> None:
        if self._last_request_at is None:
            return
        remaining = self._minimum_interval - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)


def parse_search_payload(
    payload: dict[str, Any], lookup: BookLookup
) -> tuple[MetadataCandidate, ...]:
    docs = payload.get("docs")
    if not isinstance(docs, list):
        return ()

    candidates: list[MetadataCandidate] = []
    for document in docs:
        if not isinstance(document, dict):
            continue
        work_id = _external_id(document.get("key"), "/works/")
        title = document.get("title")
        authors = document.get("author_name")
        if not work_id or not isinstance(title, str) or not isinstance(authors, list):
            continue
        author_names = tuple(name for name in authors if isinstance(name, str))
        if not author_names:
            continue

        raw_isbns = document.get("isbn", [])
        isbns = tuple(value.upper() for value in raw_isbns if isinstance(value, str))
        isbn10 = tuple(value for value in isbns if len(value) == 10)
        isbn13 = tuple(value for value in isbns if len(value) == 13)
        matched_identifier = None
        for scheme, local_value in (
            ("isbn13", lookup.isbn13),
            ("isbn10", lookup.isbn10),
        ):
            if local_value and local_value in (
                isbn13 if scheme == "isbn13" else isbn10
            ):
                matched_identifier = (scheme, local_value)
                break

        cover_value = document.get("cover_i")
        candidates.append(
            MetadataCandidate(
                external_work_id=work_id,
                external_edition_id=None,
                title=title,
                authors=author_names,
                publication_year=_integer(document.get("first_publish_year")),
                identifiers={"isbn10": isbn10, "isbn13": isbn13},
                matched_identifier=matched_identifier,
                cover_id=str(cover_value) if isinstance(cover_value, int) else None,
            )
        )
    return tuple(candidates)


def parse_metadata_payload(
    candidate: MetadataCandidate,
    work_payload: dict[str, Any],
    edition_payload: dict[str, Any] | None,
) -> BookMetadata:
    edition = edition_payload or {}
    edition_work_ids = {
        work_id
        for item in edition.get("works", [])
        if isinstance(item, dict)
        if (work_id := _external_id(item.get("key"), "/works/"))
    }
    if edition_work_ids and candidate.external_work_id not in edition_work_ids:
        raise ValueError("ISBN edition points to a different Open Library work")

    work_id = _external_id(work_payload.get("key"), "/works/")
    if work_id and work_id != candidate.external_work_id:
        raise ValueError("Fetched Open Library work does not match selected candidate")

    external_edition_id = _external_id(edition.get("key"), "/books/")
    cover_ids = edition.get("covers") or work_payload.get("covers") or []
    cover_id = (
        str(cover_ids[0]) if cover_ids and isinstance(cover_ids[0], int) else None
    )
    if cover_id is None:
        cover_id = candidate.cover_id

    published = edition.get("publish_date")
    publication_date, publication_year = _publication_date(published)
    publishers = edition.get("publishers") or []
    languages = edition.get("languages") or []

    return BookMetadata(
        external_work_id=candidate.external_work_id,
        external_edition_id=external_edition_id,
        title=_string(work_payload.get("title")) or candidate.title,
        subtitle=_string(work_payload.get("subtitle"))
        or _string(edition.get("subtitle")),
        authors=candidate.authors,
        description=_description(work_payload.get("description")),
        original_publication_year=candidate.publication_year,
        publisher=next((item for item in publishers if isinstance(item, str)), None),
        publication_date=publication_date,
        publication_year=publication_year,
        page_count=_integer(edition.get("number_of_pages")),
        language=_language(languages),
        identifiers={
            "isbn10": _string_tuple(edition.get("isbn_10")),
            "isbn13": _string_tuple(edition.get("isbn_13")),
            "oclc": _string_tuple(edition.get("oclc_numbers")),
            "lccn": _string_tuple(edition.get("lccn")),
        },
        subjects=_subject_names(work_payload.get("subjects")),
        series=_string_tuple(edition.get("series")),
        cover_id=cover_id,
        cover_urls=_cover_urls(cover_id),
    )


def _external_id(value: Any, prefix: str) -> str | None:
    if not isinstance(value, str):
        return None
    return value.removeprefix(prefix) if value.startswith(prefix) else None


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        item.strip() for item in value if isinstance(item, str) and item.strip()
    )


def _description(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("value")
    return _string(value)


def _subject_names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    names: list[str] = []
    for item in value:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return tuple(dict.fromkeys(names))


def _language(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    for item in value:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if isinstance(key, str) and key.startswith("/languages/"):
            return key.removeprefix("/languages/")
    return None


def _publication_date(value: Any) -> tuple[date | None, int | None]:
    if not isinstance(value, str):
        return None, None
    stripped = value.strip()
    for pattern in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"):
        try:
            parsed = date.fromisoformat(stripped) if pattern == "%Y-%m-%d" else None
            if parsed is not None:
                return parsed, parsed.year
        except ValueError:
            pass
        if pattern != "%Y-%m-%d":
            from datetime import datetime

            try:
                parsed = datetime.strptime(stripped, pattern).date()
                return parsed, parsed.year
            except ValueError:
                pass
    year_match = stripped[-4:]
    return (None, int(year_match)) if year_match.isdigit() else (None, None)


def _cover_urls(cover_id: str | None) -> dict[str, str]:
    if cover_id is None:
        return {}
    root = f"https://covers.openlibrary.org/b/id/{cover_id}"
    return {
        "small": f"{root}-S.jpg?default=false",
        "medium": f"{root}-M.jpg?default=false",
        "large": f"{root}-L.jpg?default=false",
    }


def _lookup_title(title: str) -> str:
    return re.sub(r"\s*\([^)]*#[^)]*\)\s*$", "", title).strip()
