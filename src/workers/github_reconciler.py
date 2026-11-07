from sqlalchemy import text

from src.database.connection import db
from src.database.types import utc_now
from src.services.artifact_service import artifact_service


async def reconcile() -> int:
    if not artifact_service.github.configured:
        return 0
    return await artifact_service.reconcile_open()


async def process_pending_webhooks() -> int:
    if not artifact_service.github.configured:
        return 0
    async with db.engine.connect() as conn:
        deliveries = (
            (
                await conn.execute(
                    text(
                        "SELECT delivery_id,pull_request_number FROM github_deliveries WHERE processing_status='pending' ORDER BY received_at LIMIT 20"
                    )
                )
            )
            .mappings()
            .all()
        )
    for delivery in deliveries:
        try:
            if delivery["pull_request_number"]:
                await artifact_service.reconcile_one(delivery["pull_request_number"])
            status, error = "processed", None
        except Exception as exc:
            status, error = "failed", type(exc).__name__
        async with db.write_tx() as tx:
            await tx.execute(
                text(
                    "UPDATE github_deliveries SET processed_at=:now,processing_status=:status,error_detail=:error WHERE delivery_id=:id"
                ),
                {"now": utc_now(), "status": status, "error": error, "id": delivery["delivery_id"]},
            )
    return len(deliveries)
