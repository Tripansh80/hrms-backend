"""Redis sliding-window rate limiter.

Limits from implement.md M11: auth 10/min/IP, AI 20/min/user, imports 5/hour/tenant, default 300/min/user.
"""

import time
import uuid

from app.core.config import get_settings
from app.core.errors import RateLimited
from app.core.redis import get_redis


async def hit(key: str, limit: int, window_seconds: int) -> None:
    """Record one hit for `key`; raise RateLimited if more than `limit` hits fall in the window."""
    if not get_settings().redis_enabled:
        return
    redis = get_redis()
    now = time.time()
    rkey = f"rl:{key}"
    async with redis.pipeline(transaction=True) as pipe:
        pipe.zremrangebyscore(rkey, 0, now - window_seconds)
        pipe.zadd(rkey, {uuid.uuid4().hex: now})
        pipe.zcard(rkey)
        pipe.expire(rkey, window_seconds)
        _, _, count, _ = await pipe.execute()
    if count > limit:
        raise RateLimited(details={"retry_after_seconds": window_seconds})
