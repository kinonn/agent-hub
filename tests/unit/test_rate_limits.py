from __future__ import annotations

from types import SimpleNamespace

import pytest

import src.api.dependencies as dependencies
import src.services.auth_service as auth_module
from src.api.dependencies import SlidingWindowLimiter
from src.errors import AgentHubError


@pytest.mark.asyncio
async def test_limiter_enforces_maximum_and_recovers_after_window(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(dependencies, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    limiter = SlidingWindowLimiter()

    await limiter.check("a", 2)
    await limiter.check("a", 2)
    with pytest.raises(AgentHubError) as error:
        await limiter.check("a", 2)
    assert error.value.status == 429

    clock[0] += 61
    await limiter.check("a", 2)


@pytest.mark.asyncio
async def test_limiter_drops_least_recently_used_keys_past_the_cap():
    limiter = SlidingWindowLimiter(max_keys=3)
    for key in ["a", "b", "c", "d"]:
        await limiter.check(key, 10)
    assert list(limiter._requests) == ["b", "c", "d"]

    await limiter.check("b", 10)
    await limiter.check("e", 10)
    assert list(limiter._requests) == ["d", "b", "e"]


@pytest.mark.asyncio
async def test_login_buckets_are_capped(harness, monkeypatch):
    monkeypatch.setattr(auth_module, "MAX_LOGIN_BUCKETS", 5)
    auth = harness["auth"]
    for i in range(8):
        with pytest.raises(AgentHubError):
            await auth.login(f"nobody{i}@example.com", "wrong-password-value", "10.0.0.1", "test")
    assert len(auth.login_attempts) == 5
