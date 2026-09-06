"""FastAPI application for the local library browser."""

from collections.abc import Iterator
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker
from starlette.datastructures import QueryParams

from book_engine.db import SessionLocal
from book_engine.web.queries import (
    SORTS,
    STATUS_LABELS,
    get_book_detail,
    get_library_page,
    get_provenance,
)
from book_engine.web.recommendations import (
    get_recommendation_center,
    record_recommendation_feedback,
)
from book_engine.web.viewmodels import LibraryFilters

WEB_ROOT = Path(__file__).parent


def create_app(session_factory: sessionmaker[Session] = SessionLocal) -> FastAPI:
    application = FastAPI(title="Book Engine", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=WEB_ROOT / "templates")
    templates.env.globals.update(
        status_label=lambda value: STATUS_LABELS.get(
            value, value.replace("_", " ").title()
        ),
        query_url=_query_url,
    )
    application.mount(
        "/static", StaticFiles(directory=WEB_ROOT / "static"), name="static"
    )

    def database_session() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    @application.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/library", status_code=307)

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/library", response_class=HTMLResponse)
    def library(
        request: Request,
        session: Annotated[Session, Depends(database_session)],
        q: str = "",
        status: str = "",
        shelf: str = "",
        year_from: Annotated[int | None, Query(ge=1, le=9999)] = None,
        year_to: Annotated[int | None, Query(ge=1, le=9999)] = None,
        rating: Annotated[int | None, Query(ge=1, le=5)] = None,
        concept: str = "",
        sort: str = "recent",
        page: Annotated[int, Query(ge=1)] = 1,
        per_page: Annotated[int, Query()] = 24,
        view: str = "grid",
    ) -> HTMLResponse:
        safe_sort = sort if sort in SORTS else "recent"
        safe_page_size = per_page if per_page in {12, 24, 48} else 24
        safe_view = view if view in {"grid", "list"} else "grid"
        filters = LibraryFilters(
            q=q.strip(),
            status=status,
            shelf=shelf,
            year_from=year_from,
            year_to=year_to,
            rating=rating,
            concept=concept,
            sort=safe_sort,
            page=page,
            per_page=safe_page_size,
            view=safe_view,
        )
        result = get_library_page(session, filters)
        return templates.TemplateResponse(
            request=request,
            name="library/index.html",
            context={"result": result},
        )

    @application.get("/recommendations", response_class=HTMLResponse)
    def recommendations(
        request: Request,
        session: Annotated[Session, Depends(database_session)],
    ) -> HTMLResponse:
        center = get_recommendation_center(session)
        return templates.TemplateResponse(
            request=request,
            name="recommendations/index.html",
            context={"center": center},
        )

    @application.post("/recommendations/{item_id}/feedback/{action}")
    def recommendation_feedback(
        item_id: int,
        action: str,
        session: Annotated[Session, Depends(database_session)],
    ) -> RedirectResponse:
        try:
            record_recommendation_feedback(session, item_id, action)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return RedirectResponse(
            f"/recommendations#recommendation-{item_id}", status_code=303
        )

    @application.get("/books/{work_id}", response_class=HTMLResponse)
    def book_detail(
        request: Request,
        work_id: int,
        session: Annotated[Session, Depends(database_session)],
    ) -> HTMLResponse:
        book = get_book_detail(session, work_id)
        if book is None:
            raise HTTPException(status_code=404, detail="Book not found")
        return templates.TemplateResponse(
            request=request, name="books/detail.html", context={"book": book}
        )

    @application.get("/books/{work_id}/provenance", response_class=HTMLResponse)
    def book_provenance(
        request: Request,
        work_id: int,
        session: Annotated[Session, Depends(database_session)],
    ) -> HTMLResponse:
        provenance = get_provenance(session, work_id)
        if provenance is None:
            raise HTTPException(status_code=404, detail="Book not found")
        return templates.TemplateResponse(
            request=request,
            name="books/provenance.html",
            context={"provenance": provenance},
        )

    return application


def _query_url(request: Request, **changes: object) -> str:
    values = dict(QueryParams(request.url.query))
    for key, value in changes.items():
        if value is None or value == "":
            values.pop(key, None)
        else:
            values[key] = str(value)
    return f"{request.url.path}?{urlencode(values)}" if values else request.url.path


app = create_app()
