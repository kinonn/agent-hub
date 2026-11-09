from __future__ import annotations

import asyncio
import contextvars
import time
from collections import OrderedDict, deque
from typing import Any

from fastapi import Request

from src.config import Settings, get_settings
from src.database.connection import Database
from src.database.connection import db as default_db
from src.errors import AgentHubError
from src.security.authentication import authenticate_agent, authenticate_session
from src.security.tokens import Principal, safe_compare

principal_context: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "agenthub_principal", default=None
)


class SlidingWindowLimiter:
    def __init__(self, max_keys: int = 10_000):
        # shortcut: least-recently-used keys are dropped past max_keys, so a flood of unique keys can reset other clients' counts; move to a shared store before running more than one process.
        self._requests: OrderedDict[str, deque[float]] = OrderedDict()
        self._max_keys = max_keys
        self._lock = asyncio.Lock()

    async def check(self, key: str, maximum: int) -> None:
        now = time.monotonic()
        async with self._lock:
            queue = self._requests.setdefault(key, deque())
            self._requests.move_to_end(key)
            while queue and queue[0] <= now - 60:
                queue.popleft()
            if len(queue) >= maximum:
                raise AgentHubError(
                    "rate_limited",
                    "Request rate limit exceeded.",
                    429,
                    headers={"Retry-After": "60"},
                )
            queue.append(now)
            while len(self._requests) > self._max_keys:
                self._requests.popitem(last=False)


rate_limiter = SlidingWindowLimiter()


async def principal_from_headers(
    headers: Any,
    database: Database | None = None,
    settings: Settings | None = None,
    *,
    allow_cookie: bool = True,
    method: str = "GET",
    cookies: dict | None = None,
) -> Principal:
    database = database or default_db
    settings = settings or get_settings()
    auth = headers.get("authorization", "") if headers else ""
    if auth:
        scheme, _, value = auth.partition(" ")
        if scheme.lower() != "bearer" or not value:
            raise AgentHubError(
                "unauthenticated",
                "A valid bearer token is required.",
                401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        principal = await authenticate_agent(database, value)
        if principal is None:
            raise AgentHubError(
                "unauthenticated",
                "A valid bearer token is required.",
                401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        await rate_limiter.check(
            f"agent:{principal.token_id}", settings.rate_limit_agent_per_minute
        )
        return principal
    if allow_cookie and cookies:
        raw_session = cookies.get("agenthub_session")
        if raw_session:
            principal = await authenticate_session(
                database, raw_session, settings.session_idle_timeout_seconds
            )
            if principal:
                if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
                    csrf = headers.get("x-csrf-token", "") if headers else ""
                    if not principal.csrf_secret or not safe_compare(csrf, principal.csrf_secret):
                        raise AgentHubError("csrf_failed", "CSRF token is missing or invalid.", 403)
                return principal
    raise AgentHubError(
        "unauthenticated",
        "Authentication is required.",
        401,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_principal(request: Request) -> Principal:
    database = getattr(request.app.state, "database", default_db)
    settings = getattr(request.app.state, "settings", get_settings())
    principal = await principal_from_headers(
        request.headers,
        database,
        settings,
        allow_cookie=True,
        method=request.method,
        cookies=request.cookies,
    )
    if principal.is_user:
        return principal
    return principal


def current_mcp_principal() -> Principal:
    principal = principal_context.get()
    if principal is None:
        raise AgentHubError(
            "unauthenticated",
            "Authentication is required.",
            401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


class MCPTransportMiddleware:
    """Applies bearer authentication and DNS-rebinding guards before MCP dispatch."""

    def __init__(self, app, database: Database | None = None, settings: Settings | None = None):
        self.app = app
        self.database = database or default_db
        self.settings = settings or get_settings()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return
        from starlette.responses import JSONResponse

        headers = {key.decode().lower(): value.decode() for key, value in scope.get("headers", [])}
        host = headers.get("host", "").lower()
        origin = headers.get("origin")
        allowed_hosts = {item.lower() for item in self.settings.allowed_hosts}
        if host not in allowed_hosts:
            response = JSONResponse(
                {"error": {"code": "forbidden", "message": "Host is not allowed."}}, status_code=403
            )
            await response(scope, receive, send)
            return
        if origin is not None and origin not in self.settings.allowed_origins:
            response = JSONResponse(
                {"error": {"code": "forbidden", "message": "Origin is not allowed."}},
                status_code=403,
            )
            await response(scope, receive, send)
            return
        try:
            client = scope.get("client")
            key = client[0] if client else "unknown"
            await rate_limiter.check(f"client:{key}", self.settings.rate_limit_anon_per_minute)
            principal = await principal_from_headers(
                headers,
                self.database,
                self.settings,
                allow_cookie=False,
                method=scope.get("method", "GET"),
            )
        except AgentHubError as error:
            response = JSONResponse(
                error.envelope(), status_code=error.status, headers=error.headers
            )
            await response(scope, receive, send)
            return
        token = principal_context.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            principal_context.reset(token)


class RequestSizeLimitMiddleware:
    def __init__(self, app, maximum_bytes: int):
        self.app = app
        self.maximum_bytes = maximum_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        length = headers.get(b"content-length")
        if length:
            try:
                too_large = int(length) > self.maximum_bytes
            except ValueError:
                too_large = True
            if too_large:
                from starlette.responses import JSONResponse

                response = JSONResponse(
                    {
                        "error": {
                            "code": "payload_too_large",
                            "message": "Request body is too large.",
                        }
                    },
                    status_code=413,
                )
                await response(scope, receive, send)
                return
        buffered = []
        size = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > self.maximum_bytes:
                    from starlette.responses import JSONResponse

                    response = JSONResponse(
                        {
                            "error": {
                                "code": "payload_too_large",
                                "message": "Request body is too large.",
                            }
                        },
                        status_code=413,
                    )
                    await response(scope, receive, send)
                    return
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                break

        index = 0

        async def replay_receive():
            nonlocal index
            if index < len(buffered):
                message = buffered[index]
                index += 1
                return message
            return await receive()

        await self.app(scope, replay_receive, send)


class ClientRateLimitMiddleware:
    """IP-based sliding window for unauthenticated and invalid-credential traffic."""

    def __init__(self, app, settings: Settings | None = None):
        self.app = app
        self.settings = settings or get_settings()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        key = client[0] if client else "unknown"
        try:
            await rate_limiter.check(f"client:{key}", self.settings.rate_limit_anon_per_minute)
        except AgentHubError as error:
            from starlette.responses import JSONResponse

            response = JSONResponse(
                error.envelope(), status_code=error.status, headers=error.headers
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
