"""FastAPI application factory.

    uvicorn autorack.main:app --proxy-headers --forwarded-allow-ips='*'

All API routes live under /api. When SERVE_FRONTEND is on (the default for
local development) the static frontend is served from / by this same process,
which makes the API same-origin and lets a single host run everything.
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from starlette.types import Scope

from . import __version__
from .api import auth, orders, people, reporting, warehouse, worker
from .config import get_settings
from .db import get_db

log = logging.getLogger("autorack")

# Kept in sync with frontend/_headers (the Cloudflare Pages copy of this policy).
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
    "font-src 'self'; connect-src 'self' {api}; frame-ancestors 'none'; base-uri 'self'; "
    "form-action 'self'; manifest-src 'self'; worker-src 'self'"
)


class FrontendFiles(StaticFiles):
    """Static frontend with the cache and security headers Pages would add."""

    async def get_response(self, path: str, scope: Scope) -> Any:
        response = await super().get_response(path, scope)
        # Code and pages always revalidate, so a deploy reaches phones on next
        # load. Only icons and the vendored decoder are allowed to sit in cache.
        if path.startswith(("assets/icons/", "w/vendor/")):
            response.headers["Cache-Control"] = "public, max-age=86400"
        else:
            response.headers["Cache-Control"] = "no-cache"
        if path.endswith("sw.js"):
            response.headers["Service-Worker-Allowed"] = "/w/"
        response.headers["Content-Security-Policy"] = CSP.format(api="")
        response.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
        return response


def create_app() -> FastAPI:
    s = get_settings()
    logging.basicConfig(level=s.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    problems = s.validate_for_production()
    if problems:
        raise RuntimeError("Refusing to start with an unsafe production config:\n- " + "\n- ".join(problems))

    app = FastAPI(
        title="Autorack API",
        version=__version__,
        description="Warehouse picking verification.",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origin_list,
        allow_credentials=False,  # bearer tokens, not cookies
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Device-Token"],
        expose_headers=["Content-Disposition", "Retry-After", "X-Request-ID"],
        max_age=600,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Any:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        started = time.perf_counter()
        response = await call_next(request)
        elapsed = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = rid
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
            if elapsed > 1000:
                log.warning("slow request %s %s %.0fms rid=%s", request.method, request.url.path, elapsed, rid)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        first = errors[0] if errors else {}
        field = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        msg = first.get("msg", "Invalid request")
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "validation_error",
                    "message": f"{field}: {msg}" if field else msg,
                    "errors": [{"loc": list(e.get("loc", ())), "msg": e.get("msg")} for e in errors[:20]],
                }
            },
        )

    @app.exception_handler(IntegrityError)
    async def integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
        # Two requests raced to create the same thing. The loser gets a clean
        # 409 instead of a 500; the constraint did its job.
        log.info("integrity conflict on %s: %s", request.url.path, exc.orig)
        return JSONResponse(
            status_code=409,
            content={
                "detail": {
                    "code": "conflict",
                    "message": "That conflicts with a change made just now. Refresh and try again.",
                }
            },
        )

    @app.exception_handler(OperationalError)
    async def db_unavailable(request: Request, exc: OperationalError) -> JSONResponse:
        log.error("database unavailable: %s", exc.orig)
        return JSONResponse(
            status_code=503,
            content={"detail": {"code": "db_unavailable", "message": "The service is briefly unavailable. Try again."}},
            headers={"Retry-After": "5"},
        )

    api = APIRouter(prefix="/api")

    @api.get("/health", tags=["public"])
    def health(db: Session = Depends(get_db)) -> dict[str, Any]:
        db.execute(text("SELECT 1"))
        return {"ok": True, "version": __version__}

    @api.get("/public/config", tags=["public"])
    def public_config() -> dict[str, Any]:
        return {
            "price_cents": s.plan_price_cents,
            "currency": "usd",
            "interval": "month",
            "trial_days": s.trial_days,
            "signup_enabled": s.signup_enabled,
            "pin_length": s.pin_length,
            "version": __version__,
        }

    for module in (auth, warehouse, people, orders, reporting, worker):
        api.include_router(module.router)
    app.include_router(api)

    frontend: Path = s.frontend_dir
    if s.serve_frontend and frontend.is_dir():
        app.mount("/", FrontendFiles(directory=frontend, html=True), name="frontend")

    return app


app = create_app()
