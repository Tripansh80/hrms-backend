"""Shared Redis client and key builders.

Tenant data is always cached under a `t:{tenant_id}:` prefix — never a shared key.
"""

import uuid

from redis.asyncio import Redis

from app.core.config import get_settings

_redis: Redis | None = None


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


def tenant_slug_key(slug: str) -> str:
    return f"tenant:slug:{slug}"


def perms_key(tenant_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """Delete on every role / membership change (M1, M2). TTL 5 min is only a backstop."""
    return f"t:{tenant_id}:perms:v2:{user_id}"
