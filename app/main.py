import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI
from app.api.routes import router as api_router
from app.api.ai_interviews_routes import router as ai_interviews_router
from app.api.internal_routes import router as internal_router
from app.infra.redis_client import get_redis
from app.db.postgres import get_pool
from app.scheduling.scheduler import run_scheduler_loop
from app.scheduling.capacity import reconcile_active_set
from app.worker.supervisor import run_supervisor_loop
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("app.main")

app = FastAPI(
    title="Proxy Realtime AI Interviewer",
    description="Automated Playwright system integrated with Azure OpenAI Realtime audio frameworks."
)

app.include_router(api_router, prefix="/api")
app.include_router(ai_interviews_router, prefix="/api")
app.include_router(internal_router)  # already includes its own /internal/v1 prefix


@app.on_event("startup")
async def on_startup():
    try:
        await get_redis().ping()
        logger.info("Redis connection OK.")

        # Every previous run's active workers are unknown to this fresh
        # process (supervisor tracking is in-memory only) — prune anything
        # left stale in Redis from a prior crash/restart, or capacity stays
        # permanently jammed "full" with nothing left alive to free it.
        pruned = await reconcile_active_set()
        if pruned:
            logger.warning(f"Startup reconcile: pruned {pruned} stale active-interview entr{'y' if pruned == 1 else 'ies'}.")
    except Exception as e:
        logger.error(f"Redis is not reachable at startup ({e}); the state machine, locks, and "
                     f"capacity/scheduling all depend on it — interviews will fail to schedule.")

    try:
        await get_pool()
        logger.info("Postgres connection pool OK.")
    except Exception as e:
        logger.error(f"Postgres is not reachable at startup ({e}); Module 2's ai_interviews/sessions/"
                     f"transcript/events tables are unavailable — scheduling will fail.")

    # Background loops for the whole worker-pool lifecycle: the scheduler
    # pre-warms/dispatches due interviews within capacity, the supervisor
    # enforces each worker's hard timeout and reaps finished processes.
    asyncio.create_task(run_scheduler_loop())
    asyncio.create_task(run_supervisor_loop())


@app.get("/health")
def health_check():
    return {"status": "operational"}