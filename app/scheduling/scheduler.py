import asyncio
import json
import logging
import time

from app.config import get_settings
from app.infra.redis_client import get_redis
from app.state import machine as state_machine
from app.state.machine import InterviewState
from app.scheduling.capacity import can_admit_now, mark_active, active_count
from app.worker.supervisor import start_worker

logger = logging.getLogger("app.scheduling.scheduler")
settings = get_settings()

SCHEDULE_KEY = "capacity:schedule"


async def schedule_interview(interview_id: str, scheduled_start_epoch: float) -> None:
    """Adds an already-`init_state()`'d interview to the pre-warm schedule.
    Existing "start immediately" callers pass time.time() as the score, so
    they're still capacity-gated but otherwise behave the same as before."""
    r = get_redis()
    await r.zadd(SCHEDULE_KEY, {interview_id: scheduled_start_epoch})


async def run_scheduler_loop() -> None:
    """Single, deliberately simple polling loop (no external scheduler
    library) — sufficient at "handful of concurrent interviews" scale. Pulls
    interviews due for pre-warming, capacity-gates them, and spawns their
    worker process."""
    r = get_redis()
    while True:
        try:
            now = time.time()
            due_before = now + settings.prewarm_lead_seconds
            due_ids = await r.zrangebyscore(SCHEDULE_KEY, 0, due_before)

            for interview_id in due_ids:
                if not await can_admit_now():
                    logger.info(
                        f"Capacity full ({await active_count()}/{settings.max_concurrent_interviews} active); "
                        f"deferring interview {interview_id} to the next tick."
                    )
                    continue

                state = await state_machine.get_state(interview_id)
                if state is None or state.get("state") != InterviewState.SCHEDULED.value:
                    # Already handled (or vanished) — stop tracking it either way.
                    await r.zrem(SCHEDULE_KEY, interview_id)
                    continue

                await r.zrem(SCHEDULE_KEY, interview_id)
                await mark_active(interview_id)
                await state_machine.transition(interview_id, InterviewState.PREPARING, reason="scheduler pre-warming")

                meeting_url = state["meeting_url"]
                join_mode = state["join_mode"]
                details = json.loads(state["details_json"])
                await start_worker(interview_id, meeting_url, join_mode, details)
        except Exception:
            logger.exception("Scheduler loop iteration failed; will retry next tick.")

        await asyncio.sleep(15)
