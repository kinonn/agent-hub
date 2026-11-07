from src.services.task_service import task_service


async def reap_expired() -> int:
    return await task_service.reap_expired()
