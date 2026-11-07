"""AgentHub FastAPI application with REST and stateless MCP transports."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.api.admin_routes import router as admin_router
from src.api.auth_routes import router as auth_router
from src.api.dependencies import (
    ClientRateLimitMiddleware,
    MCPTransportMiddleware,
    RequestSizeLimitMiddleware,
    get_current_principal,
)
from src.api.domain_routes import router as domain_router
from src.api.event_routes import router as event_router
from src.config import Settings, get_settings
from src.database.checks import check_database_filesystem, check_startup
from src.database.connection import Database, db
from src.errors import AgentHubError
from src.mcp.server import mcp
from src.security.tokens import Principal
from src.services.event_service import event_service
from src.services.health_service import health_service
from src.validation import payload_too_large_from_validation

logger = logging.getLogger("agenthub")


def create_app(database: Database | None = None, settings: Settings | None = None) -> FastAPI:
    database = database or db
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.database = database
        app.state.settings = settings
        check_database_filesystem(settings)
        await database.initialize()
        await check_startup(database, settings)
        event_service.max_connections = settings.sse_max_connections
        await event_service.start()
        logger.info("AgentHub API starting in %s mode", settings.app_env)
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await event_service.stop()
            if database is not db:
                await database.close()

    app = FastAPI(
        title="AgentHub API",
        version="0.1.0",
        description="Self-hosted coordination and knowledge service for AI agents.",
        docs_url=None if settings.app_env == "production" else "/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.database = database
    app.state.settings = settings

    api = APIRouter(prefix="/api/v1")
    api.include_router(auth_router)
    api.include_router(admin_router)
    api.include_router(domain_router)
    api.include_router(event_router)
    app.include_router(api)

    @app.get("/", include_in_schema=False)
    async def root():
        return {"service": "AgentHub", "api": "/api/v1", "mcp": "/mcp"}

    @app.get("/health", include_in_schema=False)
    @app.get("/api/v1/health", include_in_schema=False)
    async def health():
        return {"status": "ok"}

    @app.get("/health/details", include_in_schema=False)
    @app.get("/api/v1/health/details", include_in_schema=False)
    async def health_details(principal: Principal = Depends(get_current_principal)):
        return await health_service.details(principal, database, settings)

    @app.api_route(
        "/api/v1/{unmatched_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def unknown_api_route(unmatched_path: str):
        raise AgentHubError("not_found", "The requested resource was not found.", 404)

    @app.exception_handler(AgentHubError)
    async def agenthub_error_handler(_request: Request, exc: AgentHubError):
        return JSONResponse(exc.envelope(), status_code=exc.status, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        app_settings = getattr(request.app.state, "settings", settings)
        size_error = payload_too_large_from_validation(exc, app_settings)
        if size_error:
            return JSONResponse(
                size_error.envelope(), status_code=size_error.status, headers=size_error.headers
            )
        return JSONResponse(
            {"error": {"code": "validation_error", "message": "Request validation failed."}},
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_request: Request, exc: StarletteHTTPException):
        code = "not_found" if exc.status_code == 404 else "bad_request"
        message = (
            "The requested resource was not found."
            if exc.status_code == 404
            else "The request could not be processed."
        )
        return JSONResponse(
            {"error": {"code": code, "message": message}},
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(OperationalError)
    async def database_unavailable_handler(_request: Request, _exc: OperationalError):
        return JSONResponse(
            {
                "error": {
                    "code": "unavailable",
                    "message": "The database is temporarily unavailable.",
                }
            },
            status_code=503,
            headers={"Retry-After": "1"},
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_request: Request, exc: Exception):
        logger.error("Unhandled request failure: %s", type(exc).__name__)
        return JSONResponse(
            {"error": {"code": "unavailable", "message": "The request could not be completed."}},
            status_code=503,
            headers={"Retry-After": "1"},
        )

    app.add_middleware(RequestSizeLimitMiddleware, maximum_bytes=settings.max_request_bytes)
    app.add_middleware(MCPTransportMiddleware, database=database, settings=settings)
    app.add_middleware(ClientRateLimitMiddleware, settings=settings)

    # FastMCP exposes /mcp in its own Starlette app. Mount last so REST routes and
    # the dashboard health endpoints are resolved before the MCP sub-application.
    app.mount("/", mcp.streamable_http_app(), name="mcp-streamable-http")
    return app


app = create_app()
