import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import time

from app.config import get_settings
from app.state import machine as state_machine
from app.state.machine import InterviewState
from app.worker.limits import apply_process_limits, release_process_limits
from app.scheduling.capacity import mark_inactive, reconcile_active_set

logger = logging.getLogger("app.worker.supervisor")
settings = get_settings()

# interview_id -> {"process": Popen, "hard_deadline": epoch, "details_path": str}
_active: dict[str, dict] = {}


def active_count() -> int:
    return len(_active)


async def start_worker(interview_id: str, meeting_url: str, join_mode: str, details: dict) -> None:
    """Spawns the interview_worker.py entrypoint as its own OS process, and
    starts tracking it for hard-timeout enforcement."""
    fd, details_path = tempfile.mkstemp(prefix=f"interview_{interview_id}_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(details, f)

    cmd = [
        sys.executable, "-m", "app.worker.interview_worker",
        "--interview-id", interview_id,
        "--meeting-url", meeting_url,
        "--join-mode", join_mode,
        "--details-file", details_path,
    ]
    proc = subprocess.Popen(cmd)
    apply_process_limits(proc.pid, max_memory_mb=1536)

    duration_minutes = details.get("duration_minutes", 30)
    total_budget_seconds = duration_minutes * 60 + settings.hard_kill_grace_seconds
    hard_deadline = time.time() + total_budget_seconds

    _active[interview_id] = {
        "process": proc,
        "hard_deadline": hard_deadline,
        "details_path": details_path,
    }
    logger.info(
        f"Spawned worker pid={proc.pid} for interview {interview_id} "
        f"(hard deadline in {total_budget_seconds}s)"
    )


async def _reap(interview_id: str) -> None:
    info = _active.pop(interview_id, None)
    if not info:
        return
    release_process_limits(info["process"].pid)
    try:
        os.remove(info["details_path"])
    except OSError:
        pass
    try:
        await mark_inactive(interview_id)
    except Exception:
        logger.warning(f"Could not mark interview {interview_id} inactive during reap.")


async def run_supervisor_loop() -> None:
    """Polls tracked worker processes: reaps finished ones (freeing capacity)
    and hard-kills anything that has exceeded its deadline — the backstop
    for hangs/deadlocks the cooperative in-process timer can't reach. Also
    periodically reconciles capacity:active_set against real liveness, as a
    second safety net beyond the one-time startup reconcile (e.g. an entry
    from a worker this same process spawned but whose exit this loop somehow
    missed, or one that isn't in `_active` at all because it predates this
    process's own startup reconcile window)."""
    tick = 0
    while True:
        tick += 1
        if tick % 6 == 0:  # ~every 60s given the 10s sleep below
            try:
                pruned = await reconcile_active_set()
                if pruned:
                    logger.warning(f"Periodic reconcile: pruned {pruned} stale active-interview entries.")
            except Exception:
                logger.exception("Periodic capacity reconcile failed.")
            try:
                from app.repositories.ai_interview_repo import expire_stale_reservations
                expired = await expire_stale_reservations()
                if expired:
                    logger.warning(f"Periodic reconcile: expired {expired} stale bot account reservation(s).")
            except Exception:
                logger.exception("Periodic reservation expiry failed.")

        for interview_id, info in list(_active.items()):
            proc: subprocess.Popen = info["process"]
            exit_code = proc.poll()
            if exit_code is not None:
                logger.info(f"Worker for interview {interview_id} exited (code={exit_code}).")
                await _reap(interview_id)
                continue

            if time.time() > info["hard_deadline"]:
                logger.warning(
                    f"Interview {interview_id} exceeded its hard deadline; killing worker pid={proc.pid}."
                )
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                try:
                    await state_machine.transition(
                        interview_id, InterviewState.FAILED, reason="hard timeout exceeded, process killed"
                    )
                except Exception:
                    logger.warning(f"Could not record FAILED state for hard-killed interview {interview_id}.")
                await _reap(interview_id)

        await asyncio.sleep(10)
