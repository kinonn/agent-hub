from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from src.api.dependencies import get_current_principal
from src.config import get_settings
from src.errors import AgentHubError, fail
from src.security.tokens import Principal
from src.services.event_service import event_service
from src.services.github_webhook_service import github_webhook_service

router = APIRouter(tags=["events"])


@router.get("/events")
async def poll_events(
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=100),
    project: str | None = None,
    principal: Principal = Depends(get_current_principal),
):
    return await event_service.events_after(principal, after_seq, limit, project)


@router.get("/events/outbox")
async def failed_outbox_events(
    limit: int = Query(default=25, ge=1, le=100),
    principal: Principal = Depends(get_current_principal),
):
    return await event_service.failed_outbox_events(principal, limit)


@router.get("/events/stream")
async def event_stream(
    request: Request,
    project: str | None = None,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    principal: Principal = Depends(get_current_principal),
):
    raw_cursor = last_event_id or request.query_params.get("after_seq", "0")
    try:
        cursor = max(0, int(raw_cursor))
    except ValueError as exc:
        raise fail("validation_error", "Event cursor must be a non-negative integer.", 422) from exc
    try:
        subscriber = await event_service.subscribe(principal, project)
    except RuntimeError as exc:
        raise AgentHubError(
            "rate_limited", "Too many event streams.", 429, headers={"Retry-After": "30"}
        ) from exc

    async def generate():
        last_seq = cursor
        try:
            for _ in range(100):
                page = await event_service.events_after(principal, last_seq, 100, project)
                if page["resync_required"]:
                    data = json.dumps(
                        {"reason": "event_retention", "reload": True}, separators=(",", ":")
                    )
                    yield f"event: resync\ndata: {data}\n\n"
                for event in page["events"]:
                    if event["seq"] <= last_seq:
                        continue
                    encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {event['seq']}\nevent: {event['event_type']}\ndata: {encoded}\n\n"
                if page["next_seq"] == last_seq:
                    break
                last_seq = page["next_seq"]
                if len(page["events"]) < 100 and not page["resync_required"]:
                    # One extra pass confirms there is no more backlog; the shared tailer covers new events.
                    continue
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(subscriber.queue.get(), timeout=15)
                except TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if event.get("event") == "resync":
                    data = json.dumps(
                        {"reason": "subscriber_overflow", "reload": True}, separators=(",", ":")
                    )
                    yield f"id: {event['seq']}\nevent: resync\ndata: {data}\n\n"
                    last_seq = max(last_seq, int(event["seq"]))
                    continue
                if int(event["seq"]) <= last_seq:
                    continue
                last_seq = int(event["seq"])
                encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                yield f"id: {event['seq']}\nevent: {event['event_type']}\ndata: {encoded}\n\n"
        finally:
            event_service.unsubscribe(subscriber)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/webhooks/github", status_code=202)
async def github_webhook(
    request: Request,
    signature: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    delivery_id: str | None = Header(default=None, alias="X-GitHub-Delivery"),
    event_name: str | None = Header(default=None, alias="X-GitHub-Event"),
):
    settings = getattr(request.app.state, "settings", get_settings())
    return await github_webhook_service.accept(
        settings,
        signature=signature,
        delivery_id=delivery_id,
        event_name=event_name,
        body=await request.body(),
        database=getattr(request.app.state, "database", None),
    )
