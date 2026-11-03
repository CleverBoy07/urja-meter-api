"""FastAPI application: wiring, error handling, docs and the web client."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings, load_settings
from app.models import Health, Problem, SyncResult
from app.portal.client import PortalClient, PortalError, PortalRateLimited, PortalUnavailable
from app.routes import meters, network
from app.routes.deps import get_service, require_api_key
from app.service import NotReady, Service
from app.store import Store

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

DESCRIPTION = """
A clean, documented REST API over the **Urja Meter Ops** portal (which has no API of its own).

* **Meters** – inventory with filtering, search, proximity and bounding-box queries.
* **Readings** – cleaned interval readings with derived consumption, or daily roll-ups.
* **Network** – the meter hierarchy (Zone › Circle › Division › Subdivision › Substation › Feeder › DT),
  reconstructed and repaired from per-meter records.
* **Insights** – data-quality report and fleet anomalies the portal can't show.

All timestamps are ISO 8601 in IST (+05:30). Errors use RFC 9457 `application/problem+json`.
If the server sets `URJA_API_KEYS`, send one in the `X-API-Key` header.
"""

OPENAPI_TAGS = [
    {"name": "Meters", "description": "Meter inventory and readings."},
    {"name": "Network", "description": "Transformers and the reconstructed network hierarchy."},
    {"name": "Insights", "description": "Data quality and fleet-level analysis."},
    {"name": "System", "description": "Health and operations."},
]


def create_app(settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        portal = PortalClient(settings, transport=transport)
        store = Store(settings.db_path)
        service = Service(settings, portal, store)
        app.state.service = service
        await service.start()
        try:
            yield
        finally:
            await service.stop()
            await portal.aclose()
            store.close()

    app = FastAPI(
        title="Urja Meter API",
        version="1.0.0",
        description=DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        dependencies=[Depends(require_api_key)],
        responses={
            401: {"model": Problem, "description": "Missing/invalid API key"},
            503: {"model": Problem, "description": "Portal unavailable or inventory not loaded yet"},
        },
    )
    app.state.settings = settings
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    _install_error_handlers(app)

    app.include_router(meters.router)
    app.include_router(network.router)

    @app.get("/v1/health", response_model=Health, tags=["System"], summary="Liveness, data freshness and portal client stats")
    def health(svc: Service = Depends(get_service)):
        meta = svc.inventory_meta()
        count = svc.store.meter_count()
        status = "starting" if meta["synced_at"] is None else ("degraded" if meta["stale"] or meta["last_sync_error"] else "ok")
        return {"status": status, "inventory": meta, "meters": count, "portal": svc.portal.stats.as_dict()}

    @app.post("/v1/admin/sync", response_model=SyncResult, tags=["System"], summary="Force an inventory refresh from the portal now")
    async def force_sync(svc: Service = Depends(get_service)):
        return await svc.sync_inventory()

    @app.get("/reference", include_in_schema=False)
    def scalar_reference():
        return HTMLResponse(SCALAR_HTML)

    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


def _problem(status: int, title: str, detail: str | None = None, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        {"type": "about:blank", "title": title, "status": status, "detail": detail},
        status_code=status,
        media_type="application/problem+json",
        headers=headers,
    )


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        titles = {401: "Unauthorized", 404: "Not Found", 422: "Unprocessable Entity"}
        return _problem(exc.status_code, titles.get(exc.status_code, "Error"), str(exc.detail), getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        detail = "; ".join(f"{'.'.join(str(p) for p in e['loc'][1:])}: {e['msg']}" for e in exc.errors())
        return _problem(422, "Invalid request", detail)

    @app.exception_handler(NotReady)
    async def not_ready(_: Request, exc: NotReady):
        return _problem(503, "Not ready", f"{exc}. Retry shortly or check /v1/health.", {"Retry-After": "10"})

    @app.exception_handler(PortalError)
    async def portal_error(_: Request, exc: PortalError):
        retry = {"Retry-After": "30"} if isinstance(exc, (PortalRateLimited, PortalUnavailable)) else None
        status = exc.status_code
        title = {404: "Not Found", 503: "Upstream portal unavailable"}.get(status, "Upstream portal error")
        return _problem(status, title, str(exc), retry)


SCALAR_HTML = """<!doctype html>
<html><head><title>Urja Meter API reference</title><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/></head>
<body><script id="api-reference" data-url="/openapi.json"></script>
<script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference@1.25.0"></script></body></html>"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
app = create_app()
