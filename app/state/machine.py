import json
import logging
import time
from enum import Enum

from app.infra.redis_client import get_redis

logger = logging.getLogger(__name__)


class InterviewState(str, Enum):
    # Success path — maps directly onto the existing two-bot Teams flow:
    # HOST_JOINING = the signed-in admitter joins first; INTERVIEWER_JOINING =
    # the guest AI interviewer bot joins and gets admitted by the admitter;
    # WAITING_FOR_CANDIDATE = the admitter watches the lobby for the real
    # candidate and admits them too.
    SCHEDULED = "SCHEDULED"
    PREPARING = "PREPARING"
    HOST_JOINING = "HOST_JOINING"
    INTERVIEWER_JOINING = "INTERVIEWER_JOINING"
    WAITING_FOR_CANDIDATE = "WAITING_FOR_CANDIDATE"
    AUDIO_CHECK = "AUDIO_CHECK"
    INTERVIEWING = "INTERVIEWING"
    CLOSING = "CLOSING"
    FINALISING_TRANSCRIPT = "FINALISING_TRANSCRIPT"
    COMPLETED = "COMPLETED"

    # Failure states — all terminal.
    MEETING_CREATION_FAILED = "MEETING_CREATION_FAILED"
    BOT_JOIN_FAILED = "BOT_JOIN_FAILED"
    CANDIDATE_NO_SHOW = "CANDIDATE_NO_SHOW"
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


TERMINAL_STATES = {
    InterviewState.COMPLETED,
    InterviewState.MEETING_CREATION_FAILED,
    InterviewState.BOT_JOIN_FAILED,
    InterviewState.CANDIDATE_NO_SHOW,
    InterviewState.TECHNICAL_FAILURE,
    InterviewState.CANCELLED,
    InterviewState.FAILED,
}

# Explicit adjacency map: an illegal transition raises rather than silently
# corrupting the persisted state.
_SUCCESS_PATH: dict[InterviewState, set[InterviewState]] = {
    InterviewState.SCHEDULED: {InterviewState.PREPARING},
    InterviewState.PREPARING: {InterviewState.HOST_JOINING, InterviewState.MEETING_CREATION_FAILED},
    InterviewState.HOST_JOINING: {InterviewState.INTERVIEWER_JOINING, InterviewState.BOT_JOIN_FAILED},
    InterviewState.INTERVIEWER_JOINING: {InterviewState.WAITING_FOR_CANDIDATE, InterviewState.BOT_JOIN_FAILED},
    InterviewState.WAITING_FOR_CANDIDATE: {InterviewState.AUDIO_CHECK, InterviewState.CANDIDATE_NO_SHOW},
    InterviewState.AUDIO_CHECK: {InterviewState.INTERVIEWING, InterviewState.TECHNICAL_FAILURE},
    InterviewState.INTERVIEWING: {InterviewState.CLOSING, InterviewState.TECHNICAL_FAILURE},
    InterviewState.CLOSING: {InterviewState.FINALISING_TRANSCRIPT},
    InterviewState.FINALISING_TRANSCRIPT: {InterviewState.COMPLETED},
}

# Every non-terminal state can also jump straight to FAILED (a crash/hard
# timeout can happen at any point) or CANCELLED (explicit cancellation is
# allowed any time before the interview actually completes).
ALLOWED_TRANSITIONS: dict[InterviewState, set[InterviewState]] = {
    state: (targets | {InterviewState.FAILED, InterviewState.CANCELLED})
    for state, targets in _SUCCESS_PATH.items()
}
for _terminal in TERMINAL_STATES:
    ALLOWED_TRANSITIONS[_terminal] = set()


def _state_key(interview_id: str) -> str:
    return f"interview:{interview_id}:state"


def _history_key(interview_id: str) -> str:
    return f"interview:{interview_id}:history"


def _heartbeat_key(interview_id: str) -> str:
    return f"interview:{interview_id}:heartbeat"


class IllegalTransitionError(Exception):
    pass


async def init_state(
    interview_id: str,
    meeting_url: str,
    join_mode: str,
    scheduled_start_epoch: float,
    details: dict,
) -> None:
    """Creates the initial SCHEDULED state for a brand new interview."""
    r = get_redis()
    await r.hset(
        _state_key(interview_id),
        mapping={
            "state": InterviewState.SCHEDULED.value,
            "updated_at": str(time.time()),
            "worker_id": "",
            "pid": "",
            "attempt": "0",
            "meeting_url": meeting_url,
            "join_mode": join_mode,
            "scheduled_start_epoch": str(scheduled_start_epoch),
            "details_json": json.dumps(details),
            "error": "",
        },
    )
    await r.xadd(
        _history_key(interview_id),
        {"from": "", "to": InterviewState.SCHEDULED.value, "ts": str(time.time()), "reason": "created", "worker_id": ""},
        maxlen=200,
        approximate=True,
    )


async def get_state(interview_id: str) -> dict | None:
    r = get_redis()
    data = await r.hgetall(_state_key(interview_id))
    return data or None


async def transition(
    interview_id: str,
    to: InterviewState,
    reason: str = "",
    worker_id: str = "",
    pid: str | None = None,
    error: str = "",
) -> None:
    """Single choke point for every state change. Validates the transition
    against ALLOWED_TRANSITIONS, updates the current-state hash, and appends
    an entry to the history stream. Raises IllegalTransitionError instead of
    silently writing an inconsistent state."""
    r = get_redis()
    current = await get_state(interview_id)
    if current is None:
        raise IllegalTransitionError(f"No existing state for interview {interview_id}; call init_state() first.")

    from_state = InterviewState(current["state"])
    if to not in ALLOWED_TRANSITIONS.get(from_state, set()):
        raise IllegalTransitionError(f"Illegal transition {from_state} -> {to} for interview {interview_id}")

    updates = {
        "state": to.value,
        "updated_at": str(time.time()),
    }
    if worker_id:
        updates["worker_id"] = worker_id
    if pid is not None:
        updates["pid"] = str(pid)
    if error:
        updates["error"] = error

    await r.hset(_state_key(interview_id), mapping=updates)
    await r.xadd(
        _history_key(interview_id),
        {"from": from_state.value, "to": to.value, "ts": str(time.time()), "reason": reason, "worker_id": worker_id},
        maxlen=200,
        approximate=True,
    )
    logger.info(f"Interview {interview_id}: {from_state.value} -> {to.value} ({reason})")


async def heartbeat(interview_id: str, ttl_seconds: int = 45) -> None:
    """Called periodically (~every 15s) by an active worker so a recovery
    sweep can distinguish 'still working' from 'died silently'."""
    r = get_redis()
    await r.set(_heartbeat_key(interview_id), str(time.time()), ex=ttl_seconds)


async def is_alive(interview_id: str) -> bool:
    r = get_redis()
    return await r.exists(_heartbeat_key(interview_id)) == 1


async def list_stuck_interviews(scan_pattern: str = "interview:*:state") -> list[str]:
    """Recovery sweep: finds every non-terminal interview whose heartbeat has
    expired, i.e. the worker died without cleanly transitioning to a
    terminal state. Returns their interview_ids."""
    r = get_redis()
    stuck = []
    cursor = 0
    while True:
        cursor, keys = await r.scan(cursor=cursor, match=scan_pattern, count=100)
        for key in keys:
            interview_id = key.split(":")[1]
            data = await r.hgetall(key)
            if not data:
                continue
            state = InterviewState(data["state"])
            if state in TERMINAL_STATES:
                continue
            if not await is_alive(interview_id):
                stuck.append(interview_id)
        if cursor == 0:
            break
    return stuck
