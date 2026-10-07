import asyncpg

from app.config import get_settings

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    """Lazily-created, process-wide asyncpg connection pool. Not an
    lru_cache like get_settings()/get_redis() because pool creation is
    itself async and must be awaited; a plain module-level singleton with a
    guard achieves the same "create once, reuse everywhere" effect."""
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=10)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
