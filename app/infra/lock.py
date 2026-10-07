import uuid
import logging
from contextlib import asynccontextmanager

from app.infra.redis_client import get_redis

logger = logging.getLogger(__name__)

# Compare-and-delete: only release a lock if it's still held by the token
# that acquired it, so a lock that expired and was re-acquired by someone
# else is never accidentally deleted out from under them.
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

# Compare-and-extend: only renew the TTL if the caller still owns the lock.
_EXTEND_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
else
    return 0
end
"""


async def acquire_lock(name: str, ttl_ms: int = 30000) -> str | None:
    """Attempts to acquire the named distributed lock. Returns the ownership
    token on success, or None if someone else already holds it."""
    r = get_redis()
    token = uuid.uuid4().hex
    acquired = await r.set(name, token, nx=True, px=ttl_ms)
    return token if acquired else None


async def release_lock(name: str, token: str) -> bool:
    r = get_redis()
    result = await r.eval(_RELEASE_LUA, 1, name, token)
    return result == 1


async def extend_lock(name: str, token: str, ttl_ms: int = 30000) -> bool:
    r = get_redis()
    result = await r.eval(_EXTEND_LUA, 1, name, token, ttl_ms)
    return result == 1


@asynccontextmanager
async def held_lock(name: str, ttl_ms: int = 30000):
    """Async context manager mirroring `async with asyncio.Lock():`, but
    backed by Redis so it works across separate OS processes. Raises
    TimeoutError if the lock can't be acquired immediately (callers should
    retry with their own backoff policy rather than blocking here, since a
    blind blocking wait would defeat the point of a lock with a TTL)."""
    token = await acquire_lock(name, ttl_ms)
    if token is None:
        raise TimeoutError(f"Could not acquire lock '{name}': already held elsewhere.")
    try:
        yield token
    finally:
        released = await release_lock(name, token)
        if not released:
            logger.warning(f"Lock '{name}' was not released (already expired or stolen).")
