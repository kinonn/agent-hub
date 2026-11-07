from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from src.config import get_settings
from src.database.checks import check_database_filesystem, check_startup
from src.database.connection import db
from src.workers.artifact_publisher import publish_due
from src.workers.github_reconciler import process_pending_webhooks, reconcile
from src.workers.lease_reaper import reap_expired
from src.workers.maintenance import maintenance
from src.workers.outbox_dispatcher import dispatch_outbox

logging.basicConfig(
    level=getattr(logging, get_settings().log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("agenthub.worker")
HEARTBEAT = Path("/run/agenthub/worker_heartbeat")


def touch_heartbeat() -> None:
    HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT.write_text(str(time.time()))


async def _loop(
    name: str,
    interval: int,
    operation: Callable[[], Awaitable[object]],
    stop: asyncio.Event,
    *,
    initial: bool = True,
) -> None:
    if not initial:
        await asyncio.sleep(interval)
    while not stop.is_set():
        started = time.monotonic()
        try:
            result = await operation()
            logger.debug("worker loop %s complete (%s)", name, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Deliberately log the exception type only; queue payloads and credentials are sensitive.
            logger.error("worker loop %s failed: %s", name, type(exc).__name__)
        touch_heartbeat()
        delay = max(0.1, interval - (time.monotonic() - started))
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass


async def _github_loop(stop: asyncio.Event) -> None:
    settings = get_settings()
    await _loop("github-reconciler", settings.github_pr_poll_interval_seconds, reconcile, stop)


async def _heartbeat_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        touch_heartbeat()
        try:
            await asyncio.wait_for(stop.wait(), timeout=10)
        except TimeoutError:
            pass


async def run() -> None:
    settings = get_settings()
    check_database_filesystem(settings)
    await db.initialize()
    await check_startup(db, settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            pass
    touch_heartbeat()
    await asyncio.gather(
        _loop("lease-reaper", settings.worker_poll_interval_seconds, reap_expired, stop),
        _loop("outbox-dispatcher", settings.outbox_poll_interval_seconds, dispatch_outbox, stop),
        _loop("artifact-publisher", 15, publish_due, stop),
        _loop(
            "github-webhooks", settings.outbox_poll_interval_seconds, process_pending_webhooks, stop
        ),
        _github_loop(stop),
        _loop("maintenance", 300, maintenance.run_once, stop),
        _heartbeat_loop(stop),
    )
    await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
