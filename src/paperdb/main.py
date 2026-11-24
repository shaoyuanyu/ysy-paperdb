from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from starlette.middleware.base import BaseHTTPMiddleware

from paperdb import __version__
from paperdb.api.v1.router import api_router
from paperdb.config import Settings, get_settings
from paperdb.db.session import dispose_engine, get_session
from paperdb.errors import ApiError
from paperdb.storage import get_storage

logger = logging.getLogger("paperdb")


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response


def _problem(
    *,
    status: int,
    code: str,
    title: str,
    detail: str | None = None,
    request_id: str | None = None,
    errors: list[dict[str, object]] | None = None,
) -> JSONResponse:
    body: dict[str, object] = {
        "type": f"https://paperdb.local/errors/{code}",
        "title": title,
        "status": status,
        "code": code,
    }
    if detail:
        body["detail"] = detail
    if request_id:
        body["requestId"] = request_id
    if errors:
        body["errors"] = errors
    return JSONResponse(status_code=status, content=body, media_type="application/problem+json")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("paperdb %s starting", __version__)
    yield
    await dispose_engine()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="paperdb",
        version=__version__,
        description="CS-native literature library API",
        lifespan=lifespan,
        docs_url="/docs" if settings.env != "prod" else None,
        redoc_url=None,
    )
    app.add_middleware(RequestIdMiddleware)
    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.include_router(api_router)

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return _problem(
            status=exc.status,
            code=exc.code,
            title=exc.title,
            detail=exc.detail,
            request_id=getattr(request.state, "request_id", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"path": ".".join(str(part) for part in error["loc"]), "message": error["msg"]}
            for error in exc.errors()
        ]
        return _problem(
            status=422,
            code="validation_failed",
            title="Validation failed",
            detail="Request body or query parameters are invalid",
            request_id=getattr(request.state, "request_id", None),
            errors=errors,
        )

    @app.exception_handler(IntegrityError)
    async def integrity_handler(request: Request, exc: IntegrityError) -> JSONResponse:
        logger.warning("integrity error: %s", exc)
        return _problem(
            status=409,
            code="conflict",
            title="Conflict",
            detail="The write conflicts with existing data",
            request_id=getattr(request.state, "request_id", None),
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        logger.exception("unhandled error request_id=%s", request_id)
        return _problem(
            status=500,
            code="internal",
            title="Internal server error",
            request_id=request_id,
        )

    @app.get("/healthz", tags=["system"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["system"])
    async def readyz() -> JSONResponse:
        session = None
        try:
            async for candidate in get_session():
                session = candidate
                break
            assert session is not None
            await session.execute(text("select 1"))
            await get_storage().healthcheck()
        except Exception as exc:
            logger.warning("readiness failed: %s", exc)
            return _problem(status=503, code="not_ready", title="Not ready", detail=str(exc))
        finally:
            if session is not None:
                await session.close()
        return JSONResponse({"status": "ready", "version": __version__})

    return app


app = create_app()
