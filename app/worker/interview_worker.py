"""Subprocess entrypoint: one OS process per interview.

Run as: python -m app.worker.interview_worker --interview-id ... --meeting-url ... --join-mode ... --details-file ...

Drives the interview through the persisted state machine and delegates the
actual Teams/Playwright/Realtime work to app.bot.engine.launch_bot() (solo
signed_in/guest_only modes) or launch_admitter_and_guest() (guest mode: one
shared browser, two tabs).
"""
import argparse
import asyncio
import json
import logging
import os
import sys

from app.bot.engine import launch_bot, launch_admitter_and_guest
from app.state import machine as state_machine
from app.state.machine import InterviewState
from app.scheduling.capacity import mark_inactive

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("app.worker")


async def _heartbeat_loop(interview_id: str, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await state_machine.heartbeat(interview_id)
        except Exception as e:
            logger.warning(f"Heartbeat write failed: {e}")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=15)
        except asyncio.TimeoutError:
            pass


async def run(interview_id: str, meeting_url: str, join_mode: str, details: dict) -> None:
    stop_hb = asyncio.Event()
    hb_task = asyncio.create_task(_heartbeat_loop(interview_id, stop_hb))

    # So app/realtime/connection.py (running inside launch_bot, in this same
    # process) can drive the AUDIO_CHECK/INTERVIEWING/CLOSING transitions
    # itself from the real Realtime tool-call events, instead of this worker
    # guessing at their timing from the outside.
    details = {**details, "interview_id": interview_id}

    try:
        # The scheduler already transitioned this interview to PREPARING
        # before spawning this process — record worker identity here on the
        # move to HOST_JOINING rather than re-entering PREPARING (which
        # would be an illegal same-state transition).
        await state_machine.transition(
            interview_id,
            InterviewState.HOST_JOINING,
            reason="worker process started; admitter dispatching first",
            worker_id=str(os.getpid()),
            pid=os.getpid(),
        )

        tasks = []
        if join_mode == "guest":
            # Admitter and guest now run as two tabs in one shared browser
            # process (see launch_admitter_and_guest) instead of each
            # launching its own separate browser.
            admitter_ready = asyncio.Event()
            tasks.append(asyncio.create_task(
                launch_admitter_and_guest(meeting_url, details, admitter_ready=admitter_ready)
            ))
            # The admitter sets this once it's actually in the meeting and
            # the guest tab may start joining — a real signal, not a guessed
            # delay.
            await admitter_ready.wait()
            await state_machine.transition(
                interview_id, InterviewState.INTERVIEWER_JOINING, reason="admitter ready; guest bot joining"
            )
        else:
            tasks.append(asyncio.create_task(
                launch_bot(meeting_url=meeting_url, details=details, join_mode=join_mode)
            ))
            await state_machine.transition(
                interview_id, InterviewState.INTERVIEWER_JOINING, reason="signed-in/guest-only bot joining"
            )

        # NOTE: this worker still doesn't have a direct signal for "guest is
        # fully in the meeting, watching for the candidate" — that one transition
        # stays best-effort/approximate here. AUDIO_CHECK, INTERVIEWING, and
        # CLOSING are real, driven by connection.py's tool-call handlers via
        # the interview_id now threaded through `details` above.
        await state_machine.transition(
            interview_id, InterviewState.WAITING_FOR_CANDIDATE, reason="guest bot dispatched; awaiting candidate"
        )

        await asyncio.gather(*tasks)

        # connection.py should have already driven CLOSING once the call
        # ended; if the process reaches here from some other path (e.g. the
        # guest never got far enough for connection.py to run), only
        # attempt the remaining transitions if they're still legal from
        # whatever state we're actually in.
        current = await state_machine.get_state(interview_id)
        current_state = InterviewState(current["state"]) if current else None
        if current_state not in state_machine.TERMINAL_STATES:
            if current_state != InterviewState.CLOSING:
                await state_machine.transition(interview_id, InterviewState.CLOSING, reason="bot tasks completed")
            await state_machine.transition(
                interview_id, InterviewState.FINALISING_TRANSCRIPT, reason="wrapping up transcript"
            )
            await state_machine.transition(interview_id, InterviewState.COMPLETED, reason="worker finished cleanly")
    except Exception as e:
        logger.exception("Worker failed")
        try:
            await state_machine.transition(interview_id, InterviewState.FAILED, reason=str(e))
        except Exception:
            logger.error("Could not record FAILED state either.")
    finally:
        stop_hb.set()
        hb_task.cancel()
        try:
            await mark_inactive(interview_id)
        except Exception:
            logger.warning("Could not mark interview inactive in capacity tracking.")
        # Whether this run succeeded or crashed, free the bot account
        # reservation -- nothing else releases it once the worker actually
        # starts, and expire_stale_reservations() alone doesn't help here
        # since the reservation's time window can still be in the future
        # relative to "now" even though this interview is already done.
        try:
            from app.repositories.ai_interview_repo import get_ai_interview_by_redis_id, release_reservation_for_ai_interview
            ai_interview = await get_ai_interview_by_redis_id(interview_id)
            if ai_interview:
                await release_reservation_for_ai_interview(ai_interview["id"])
        except Exception:
            logger.warning("Could not release bot account reservation.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interview-id", required=True)
    parser.add_argument("--meeting-url", required=True)
    parser.add_argument("--join-mode", default="guest")
    parser.add_argument("--details-file", required=True)
    args = parser.parse_args()

    with open(args.details_file, "r", encoding="utf-8") as f:
        details = json.load(f)

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    asyncio.run(run(args.interview_id, args.meeting_url, args.join_mode, details))


if __name__ == "__main__":
    main()
