from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from src.api.dependencies import get_current_principal
from src.schemas.models import (
    ClaimTaskBody,
    CompleteTaskBody,
    CreateTaskBody,
    HeartbeatTaskBody,
    MessageReadInput,
    PublishArtifactBody,
    PublishMessageBody,
    UpdateTaskBody,
)
from src.security.tokens import Principal
from src.services.agent_service import agent_service
from src.services.artifact_service import artifact_service
from src.services.message_service import message_service
from src.services.task_service import task_service

router = APIRouter(tags=["agent operations"])


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, principal: Principal = Depends(get_current_principal)):
    return await agent_service.get_agent_status(principal, agent_id)


@router.get("/projects/{project_ref}/agents")
async def list_project_agents(
    project_ref: str, principal: Principal = Depends(get_current_principal)
):
    return await agent_service.list_agents(principal, project_ref)


@router.get("/projects/{project_ref}/messages")
async def get_messages(
    project_ref: str,
    unread_only: bool = False,
    thread_id: str | None = None,
    since: str | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = None,
    principal: Principal = Depends(get_current_principal),
):
    return await message_service.get_messages(
        principal,
        project_ref,
        unread_only=unread_only,
        thread_id=thread_id,
        since=since,
        limit=limit,
        cursor=cursor,
    )


@router.post("/projects/{project_ref}/messages", status_code=201)
async def publish_message(
    project_ref: str,
    body: PublishMessageBody,
    principal: Principal = Depends(get_current_principal),
):
    return await message_service.publish_message(
        principal,
        project_ref,
        body.subject,
        body.body,
        body.message_type,
        body.recipient_agent_id,
        body.thread_id,
        body.idempotency_key,
    )


@router.post("/messages/{message_id}/read")
async def mark_message_read(
    message_id: str, body: MessageReadInput, principal: Principal = Depends(get_current_principal)
):
    return await message_service.mark_message_read(principal, message_id, body.acknowledge)


@router.get("/projects/{project_ref}/tasks")
async def get_tasks(
    project_ref: str,
    claimable_only: bool = False,
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = None,
    principal: Principal = Depends(get_current_principal),
):
    return await task_service.get_pending_tasks(
        principal,
        project_ref,
        claimable_only=claimable_only,
        limit=limit,
        cursor=cursor,
    )


@router.post("/projects/{project_ref}/tasks", status_code=201)
async def create_task(
    project_ref: str,
    body: CreateTaskBody,
    principal: Principal = Depends(get_current_principal),
):
    return await task_service.create_task(
        principal,
        project_ref,
        body.task_type,
        body.title,
        body.description,
        body.input_payload,
        body.assigned_agent_id,
        body.target_capability,
        body.priority,
        body.available_at,
        body.max_attempts,
        body.idempotency_key,
    )


@router.post("/projects/{project_ref}/tasks/claim")
async def claim_task(
    project_ref: str,
    body: ClaimTaskBody,
    principal: Principal = Depends(get_current_principal),
):
    result = await task_service.claim_task(principal, project_ref, body.task_id, body.instance_id)
    if result is None:
        return Response(status_code=204)
    return result


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, principal: Principal = Depends(get_current_principal)):
    return await task_service.get_task(principal, task_id)


@router.get("/tasks/{task_id}/events")
async def task_events(task_id: str, principal: Principal = Depends(get_current_principal)):
    return {"events": await task_service.task_events(principal, task_id)}


@router.patch("/tasks/{task_id}")
async def update_task(
    task_id: str, body: UpdateTaskBody, principal: Principal = Depends(get_current_principal)
):
    return await task_service.update_task(
        principal,
        task_id,
        lease_token=body.lease_token,
        status=body.status,
        progress_note=body.progress_note,
        blocked_reason=body.blocked_reason,
        error_detail=body.error_detail,
        retryable=body.retryable,
    )


@router.post("/tasks/{task_id}/heartbeat")
async def heartbeat_task(
    task_id: str, body: HeartbeatTaskBody, principal: Principal = Depends(get_current_principal)
):
    return await task_service.heartbeat_task(
        principal, task_id, body.lease_token, body.progress_note
    )


@router.post("/tasks/{task_id}/complete")
async def complete_task(
    task_id: str,
    body: CompleteTaskBody,
    principal: Principal = Depends(get_current_principal),
):
    return await task_service.complete_task(
        principal,
        task_id,
        body.lease_token,
        body.result_summary,
        body.artifact_ids,
    )


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, principal: Principal = Depends(get_current_principal)):
    return await task_service.cancel_task(principal, task_id)


@router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str, principal: Principal = Depends(get_current_principal)):
    return await task_service.retry_task(principal, task_id)


@router.get("/projects/{project_ref}/artifacts")
async def list_artifacts(
    project_ref: str,
    publication_status: str | None = Query(default=None, alias="status"),
    artifact_type: str | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = None,
    principal: Principal = Depends(get_current_principal),
):
    return await artifact_service.list_project_artifacts(
        principal,
        project_ref,
        status=publication_status,
        artifact_type=artifact_type,
        limit=limit,
        cursor=cursor,
    )


@router.post("/projects/{project_ref}/artifacts", status_code=201)
async def publish_artifact(
    project_ref: str,
    body: PublishArtifactBody,
    principal: Principal = Depends(get_current_principal),
):
    return await artifact_service.publish_artifact(
        principal,
        project_ref,
        body.artifact_type,
        body.title,
        body.content,
        summary=body.summary,
        document_key=body.document_key,
        task_id=body.task_id,
        lease_token=body.lease_token,
        idempotency_key=body.idempotency_key,
    )


@router.get("/artifacts/{artifact_id}")
async def get_artifact(
    artifact_id: str,
    include_content: bool = False,
    principal: Principal = Depends(get_current_principal),
):
    return await artifact_service.get_artifact(principal, artifact_id, include_content)


@router.get("/projects/{project_ref}/search")
async def search_knowledge(
    project_ref: str,
    q: str = Query(min_length=1, max_length=500),
    artifact_type: str | None = None,
    include_unpublished: bool = False,
    limit: int = Query(default=10, ge=1, le=20),
    principal: Principal = Depends(get_current_principal),
):
    return await artifact_service.search_knowledge(
        principal,
        project_ref,
        q,
        artifact_type=artifact_type,
        include_unpublished=include_unpublished,
        limit=limit,
    )
