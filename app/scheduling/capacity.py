import logging

from app.config import get_settings
from app.infra.redis_client import get_redis
from app.state import machine as state_machine
from app.state.machine import InterviewState, TERMINAL_STATES

logger = logging.getLogger("app.scheduling.capacity")
settings = get_settings()

ACTIVE_SET_KEY = "capacity:active_set"


async def can_admit_now() -> bool:
    r = get_redis()
    count = await r.scard(ACTIVE_SET_KEY)
    return count < settings.max_concurrent_interviews


async def active_count() -> int:
    r = get_redis()
    return await r.scard(ACTIVE_SET_KEY)


async def mark_active(interview_id: str) -> None:
    r = get_redis()
    await r.sadd(ACTIVE_SET_KEY, interview_id)


async def mark_inactive(interview_id: str) -> None:
    r = get_redis()
    await r.srem(ACTIVE_SET_KEY, interview_id)


async def reconcile_active_set() -> int:
    """Prunes stale entries left behind by a worker that died (or the app
    itself restarted) without ever reaching mark_inactive(). The supervisor's
    process-tracking is in-memory only and doesn't survive a restart, but
    Redis's active_set does — without this, capacity silently jams at "full"
    forever after any crash/restart. Called once at API startup and safe to
    call periodically. Returns how many entries were pruned.
    """
    r = get_redis()
    members = await r.smembers(ACTIVE_SET_KEY)
    pruned = 0
    for interview_id in members:
        state = await state_machine.get_state(interview_id)
        is_terminal = state is not None and state.get("state") in {s.value for s in TERMINAL_STATES}
        is_dead = not await state_machine.is_alive(interview_id)

        if state is None or is_terminal or is_dead:
            if state is not None and not is_terminal:
                try:
                    await state_machine.transition(
                        interview_id, InterviewState.FAILED, reason="reconcile: no heartbeat, worker presumed dead"
                    )
                except Exception:
                    pass
            await r.srem(ACTIVE_SET_KEY, interview_id)
            pruned += 1
            logger.warning(f"Reconcile: pruned stale active_set entry for interview {interview_id}.")

            # A dead-worker-process case (the actual OS process was killed
            # or orphaned by a restart) never runs interview_worker.py's own
            # finally block, which is where a clean run releases the bot
            # account reservation -- so it has to happen here too, or the
            # reservation is permanently stuck.
            try:
                from app.repositories.ai_interview_repo import get_ai_interview_by_redis_id, release_reservation_for_ai_interview
                ai_interview = await get_ai_interview_by_redis_id(interview_id)
                if ai_interview:
                    await release_reservation_for_ai_interview(ai_interview["id"])
            except Exception:
                logger.warning(f"Reconcile: could not release bot reservation for interview {interview_id}.")
    return pruned
