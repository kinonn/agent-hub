from src.services.artifact_service import artifact_service


async def publish_due() -> int:
    return await artifact_service.publish_due()
