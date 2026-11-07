from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from src.api.dependencies import get_current_principal
from src.config import get_settings
from src.schemas.admin import LoginRequest
from src.security.tokens import Principal
from src.services.auth_service import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response):
    ip = request.client.host if request.client else None
    result = await auth_service.login(
        body.email, body.password, ip, request.headers.get("user-agent")
    )
    settings = getattr(request.app.state, "settings", get_settings())
    response.set_cookie(
        "agenthub_session",
        result["session_token"],
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
        max_age=settings.session_absolute_timeout_seconds,
    )
    return {"user": result["user"], "csrf_token": result["csrf_token"]}


@router.post("/logout")
async def logout(
    request: Request, response: Response, principal: Principal = Depends(get_current_principal)
):
    await auth_service.logout(principal)
    settings = getattr(request.app.state, "settings", get_settings())
    response.delete_cookie(
        "agenthub_session", path="/", secure=settings.cookie_secure, httponly=True, samesite="lax"
    )
    return {"status": "ok"}


@router.get("/me")
async def me(principal: Principal = Depends(get_current_principal)):
    if principal.is_agent:
        return {"type": "agent", "id": principal.id, "name": principal.name}
    return {
        "type": "user",
        "id": principal.id,
        "display_name": principal.name,
        "role": principal.role,
        "csrf_token": principal.csrf_secret,
    }
