from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status

from src.api.dependencies import get_current_principal
from src.schemas.admin import (
    AgentMembershipRequest,
    CreateAgentRequest,
    CreateProjectRequest,
    CreateUserRequest,
    PatchAgentRequest,
    PatchProjectRequest,
    PatchUserRequest,
    UserMembershipRequest,
)
from src.security.tokens import Principal
from src.services.auth_service import auth_service

router = APIRouter(tags=["administration"])


@router.get("/users")
async def list_users(principal: Principal = Depends(get_current_principal)):
    return {"users": await auth_service.list_users(principal)}


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(
    body: CreateUserRequest, principal: Principal = Depends(get_current_principal)
):
    return await auth_service.create_user(
        body.email, body.password, body.display_name, body.role, actor=principal
    )


@router.patch("/users/{user_id}")
async def patch_user(
    user_id: str, body: PatchUserRequest, principal: Principal = Depends(get_current_principal)
):
    return await auth_service.patch_user(user_id, body.model_dump(exclude_unset=True), principal)


@router.get("/agents")
async def list_agents_admin(principal: Principal = Depends(get_current_principal)):
    return {"agents": await auth_service.list_agents_admin(principal)}


@router.post("/agents", status_code=status.HTTP_201_CREATED)
async def create_agent(
    body: CreateAgentRequest, principal: Principal = Depends(get_current_principal)
):
    return await auth_service.create_agent(
        body.name,
        body.agent_type,
        body.description,
        body.capabilities,
        body.execution_mode,
        body.adapter_config,
        principal,
    )


@router.patch("/agents/{agent_id}")
async def patch_agent(
    agent_id: str, body: PatchAgentRequest, principal: Principal = Depends(get_current_principal)
):
    return await auth_service.patch_agent(agent_id, body.model_dump(exclude_unset=True), principal)


@router.post("/agents/{agent_id}/tokens")
async def rotate_token(agent_id: str, principal: Principal = Depends(get_current_principal)):
    return await auth_service.rotate_agent_token(agent_id, principal)


@router.delete("/agents/{agent_id}/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_token(
    agent_id: str, token_id: str, principal: Principal = Depends(get_current_principal)
):
    await auth_service.revoke_agent_token(agent_id, token_id, principal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/projects")
async def list_projects(principal: Principal = Depends(get_current_principal)):
    return {"projects": await auth_service.list_projects(principal)}


@router.post("/projects", status_code=status.HTTP_201_CREATED)
async def create_project(
    body: CreateProjectRequest, principal: Principal = Depends(get_current_principal)
):
    return await auth_service.create_project(
        body.slug, body.name, body.description, body.instructions, principal
    )


@router.patch("/projects/{project_ref}")
async def patch_project(
    project_ref: str,
    body: PatchProjectRequest,
    principal: Principal = Depends(get_current_principal),
):
    return await auth_service.patch_project(
        project_ref, body.model_dump(exclude_unset=True), principal
    )


@router.get("/projects/{project_ref}/members/{agent_id}")
async def get_agent_membership(
    project_ref: str, agent_id: str, principal: Principal = Depends(get_current_principal)
):
    return await auth_service.get_agent_membership(project_ref, agent_id, principal)


@router.put("/projects/{project_ref}/members/{agent_id}")
async def put_agent_membership(
    project_ref: str,
    agent_id: str,
    body: AgentMembershipRequest,
    principal: Principal = Depends(get_current_principal),
):
    return await auth_service.set_agent_membership(project_ref, agent_id, body.role, principal)


@router.delete("/projects/{project_ref}/members/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_membership(
    project_ref: str, agent_id: str, principal: Principal = Depends(get_current_principal)
):
    await auth_service.set_agent_membership(project_ref, agent_id, None, principal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/projects/{project_ref}/users/{user_id}")
async def get_user_membership(
    project_ref: str, user_id: str, principal: Principal = Depends(get_current_principal)
):
    return await auth_service.get_user_membership(project_ref, user_id, principal)


@router.put("/projects/{project_ref}/users/{user_id}")
async def put_user_membership(
    project_ref: str,
    user_id: str,
    body: UserMembershipRequest,
    principal: Principal = Depends(get_current_principal),
):
    return await auth_service.set_user_membership(project_ref, user_id, body.role, principal)


@router.delete("/projects/{project_ref}/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user_membership(
    project_ref: str, user_id: str, principal: Principal = Depends(get_current_principal)
):
    await auth_service.set_user_membership(project_ref, user_id, None, principal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/projects/{project_ref}/members")
async def list_project_members(
    project_ref: str, principal: Principal = Depends(get_current_principal)
):
    return {"agents": await auth_service.list_agent_memberships(project_ref, principal)}


@router.get("/audit")
async def list_audit(
    limit: int = Query(default=100, ge=1, le=100),
    after: str | None = None,
    principal: Principal = Depends(get_current_principal),
):
    return {"audit": await auth_service.list_audit(principal, limit, after)}
