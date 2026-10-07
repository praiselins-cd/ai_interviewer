"""Business logic for Module 2 scheduling/cancel/dev-start. Routes stay thin
and call into this module; this module is the only thing that talks to the
repository layer, the meeting provider, and the Redis state machine/scheduler
together.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.config import get_settings
from app.db.postgres import get_pool
from app.repositories import ai_interview_repo
from app.services.meeting_provider import TeamsMeetingProvider
from app.state import machine as state_machine
from app.state.machine import TERMINAL_STATES, InterviewState
from app.scheduling.scheduler import schedule_interview as redis_schedule_interview
from app.infra.redis_client import get_redis

logger = logging.getLogger("app.services.ai_interview")
settings = get_settings()
meeting_provider = TeamsMeetingProvider()

RESERVATION_MINUTES = 20


class PlanNotFoundError(Exception):
    pass


class AiInterviewNotFoundError(Exception):
    pass


class PastScheduleError(Exception):
    """Raised when `scheduled_start` is in the past and the caller didn't
    explicitly opt out of that check via `allow_past_start` -- that opt-out
    exists purely for manual testing (so you can schedule "now" and watch it
    dispatch immediately instead of waiting for a real future time)."""


def _bot_account_email() -> str:
    return settings.bot_account_email or settings.teams_user_email


def _load_jsonb(value):
    """asyncpg returns JSONB columns as raw JSON text (no codec registered in
    app/db/postgres.py), so every JSONB column read here needs an explicit
    json.loads -- mirrors how ai_interview_repo.py's writers json.dumps
    JSONB params rather than relying on asyncpg to serialize dicts."""
    return json.loads(value) if isinstance(value, str) else value


async def load_assessment_plan(assessment_plan_id: UUID, assessment_plan_version: int) -> dict:
    """Reads a frozen plan version from Module 1's `ai_assessment_plans` table
    (service/db/models_assessment_plan.py) -- now that `ai interviewer` points
    at the same Postgres database as `service/`, this queries it directly via
    the same asyncpg pool this module already uses, rather than through
    Module 1's own SQLAlchemy layer.

    Returns a dict shaped like:
    {
        "candidate_name": str, "position_title": str, "duration_minutes": int,
        "competencies": [str, ...], "indicators": [str, ...],
        "questions": [{"id": "q1", "text": "..."}, ...],  # exactly 4
        "resume_claim": str,
    }
    Raises PlanNotFoundError instead of silently proceeding with
    fabricated/placeholder data.
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT duration_minutes, plan_json, job_snapshot_json, candidate_summary_snapshot_json
        FROM ai_assessment_plans
        WHERE id = $1 AND version = $2
        """,
        assessment_plan_id,
        assessment_plan_version,
    )
    if row is None:
        raise PlanNotFoundError(
            f"No assessment plan found for id={assessment_plan_id} version={assessment_plan_version}"
        )

    plan_json = _load_jsonb(row["plan_json"])
    job_snapshot = _load_jsonb(row["job_snapshot_json"])
    candidate_snapshot = _load_jsonb(row["candidate_summary_snapshot_json"])

    competencies = plan_json.get("competencies", [])
    questions = plan_json.get("questions", [])
    resume_question = next((q for q in questions if q.get("type") == "RESUME_BASED"), None)

    return {
        "candidate_name": candidate_snapshot.get("full_name") or "Candidate",
        "position_title": job_snapshot.get("job_title"),
        "duration_minutes": row["duration_minutes"],
        "competencies": [c.get("name") for c in competencies],
        "indicators": [i for c in competencies for i in c.get("assessment_indicators", [])],
        "questions": [{"id": q.get("question_id"), "text": q.get("question")} for q in questions],
        "resume_claim": resume_question.get("question", "") if resume_question else "",
    }


async def _resolve_stale_existing(existing):
    """Checks an idempotency candidate's real Redis state; returns it
    unchanged if genuinely still live/non-terminal, or None (meaning the
    caller should proceed to create a fresh attempt) if Redis shows it's
    actually dead -- syncing Postgres's `status` to match first, so the
    next lookup doesn't hit the same stale state."""
    redis_interview_id = existing["redis_interview_id"]
    if not redis_interview_id:
        # Never even got as far as Redis init (shouldn't normally happen,
        # since MEETING_CREATION_FAILED etc are already excluded by the
        # query) -- treat as stale rather than trusting it.
        await ai_interview_repo.update_ai_interview_status(existing["id"], InterviewState.FAILED.value)
        return None

    state = await state_machine.get_state(redis_interview_id)
    if state is None:
        await ai_interview_repo.update_ai_interview_status(existing["id"], InterviewState.FAILED.value)
        return None

    redis_status = state.get("state")
    if redis_status in TERMINAL_STATES:
        if redis_status != existing["status"]:
            await ai_interview_repo.update_ai_interview_status(existing["id"], redis_status)
        return existing if redis_status == InterviewState.COMPLETED.value else None

    if not await state_machine.is_alive(redis_interview_id):
        await ai_interview_repo.update_ai_interview_status(existing["id"], InterviewState.FAILED.value)
        return None

    # Non-terminal and heartbeat alive -- genuinely still in progress.
    return existing


async def schedule_ai_interview(
    assessment_plan_id: UUID,
    assessment_plan_version: int,
    scheduled_start: datetime,
    timezone_name: str,
    allow_past_start: bool = False,
    existing_meeting_url: str | None = None,
) -> dict:
    now = datetime.now(scheduled_start.tzinfo) if scheduled_start.tzinfo else datetime.now()
    if scheduled_start < now and not allow_past_start:
        raise PastScheduleError(
            f"scheduled_start {scheduled_start.isoformat()} is in the past (now: {now.isoformat()}). "
            "Pass allow_past_start=true to bypass this for manual testing."
        )

    # Idempotency: a non-terminal ai_interviews row already exists for this
    # exact plan id+version — return it rather than creating a duplicate
    # meeting/reservation. Postgres's `status` column is only ever written
    # at a few specific points (creation, and a couple of early failure
    # paths below) -- nothing mirrors the ongoing Redis state-machine
    # transitions (PREPARING -> ... -> FAILED/COMPLETED) back into it, so a
    # row can sit at a stale `status` (e.g. SCHEDULED) forever after the
    # real Redis-tracked interview has already reached a terminal state or
    # died silently. Trust Redis as the live source of truth here: if it
    # disagrees with Postgres, sync Postgres to match and fall through to
    # create a fresh attempt instead of handing back a dead one.
    existing = await ai_interview_repo.find_scheduled_by_plan(assessment_plan_id, assessment_plan_version)
    if existing:
        existing = await _resolve_stale_existing(existing)
    if existing:
        logger.info(f"Idempotent schedule: plan {assessment_plan_id} v{assessment_plan_version} already scheduled.")
        return _to_schedule_response(existing)

    plan = await load_assessment_plan(assessment_plan_id, assessment_plan_version)
    duration_minutes = plan.get("duration_minutes", 15)
    scheduled_end = scheduled_start + timedelta(minutes=duration_minutes)

    ai_interview_id = await ai_interview_repo.create_ai_interview(
        assessment_plan_id=assessment_plan_id,
        assessment_plan_version=assessment_plan_version,
        assessment_plan_snapshot=plan,
        candidate_name=plan.get("candidate_name", "Candidate"),
        position_title=plan.get("position_title"),
        scheduled_start=scheduled_start,
        scheduled_end=scheduled_end,
        timezone_name=timezone_name,
        duration_minutes=duration_minutes,
    )

    reserved_from = scheduled_start
    reserved_until = scheduled_start + timedelta(minutes=RESERVATION_MINUTES)
    try:
        reservation_id = await ai_interview_repo.reserve_bot_account(
            _bot_account_email(), ai_interview_id, reserved_from, reserved_until
        )
    except ai_interview_repo.BotAccountBusyError:
        await ai_interview_repo.update_ai_interview_status(ai_interview_id, InterviewState.FAILED.value)
        raise
    await ai_interview_repo.set_bot_account_reservation(ai_interview_id, reservation_id)

    if existing_meeting_url:
        # Testing-only escape hatch: skip Graph entirely and reuse an
        # already-created meeting link, so repeated join/admit testing
        # doesn't need a fresh Graph call (and its own possible failure
        # mode) every single time -- a Teams onlineMeeting stays joinable
        # indefinitely once created, unlike a calendar event tied to a
        # specific start/end time.
        meeting = {"meeting_id": f"reused:{uuid4().hex}", "join_web_url": existing_meeting_url}
    else:
        try:
            meeting = await meeting_provider.create_meeting({
                "subject": f"AI Interview - {plan.get('candidate_name', 'Candidate')}",
                "start_iso": scheduled_start.isoformat(),
                "end_iso": scheduled_end.isoformat(),
            })
        except Exception:
            logger.exception("Graph meeting creation failed")
            await ai_interview_repo.update_ai_interview_status(ai_interview_id, InterviewState.MEETING_CREATION_FAILED.value)
            await ai_interview_repo.release_reservation(reservation_id)
            raise

    await ai_interview_repo.set_meeting_details(ai_interview_id, meeting["meeting_id"], meeting["join_web_url"])

    redis_interview_id = uuid4().hex
    await ai_interview_repo.set_redis_interview_id(ai_interview_id, redis_interview_id)
    session_id = await ai_interview_repo.create_session(ai_interview_id, session_number=1, redis_interview_id=redis_interview_id)

    details = {
        "candidate": plan.get("candidate_name", "Candidate"),
        "position_title": plan.get("position_title"),
        "assessment_plan_snapshot": plan,
        "duration_minutes": duration_minutes,
        "interview_id": redis_interview_id,
        "pg_session_id": str(session_id),
    }

    await state_machine.init_state(
        interview_id=redis_interview_id,
        meeting_url=meeting["join_web_url"],
        join_mode="guest",
        scheduled_start_epoch=scheduled_start.timestamp(),
        details=details,
    )
    await redis_schedule_interview(redis_interview_id, scheduled_start.timestamp())

    return {
        "ai_interview_id": str(ai_interview_id),
        "status": "SCHEDULED",
        "scheduled_start": scheduled_start.isoformat(),
        "scheduled_end": scheduled_end.isoformat(),
        "candidate_join_url": meeting["join_web_url"],
    }


def _to_schedule_response(row) -> dict:
    return {
        "ai_interview_id": str(row["id"]),
        "status": row["status"],
        "scheduled_start": row["scheduled_start"].isoformat(),
        "scheduled_end": row["scheduled_end"].isoformat(),
        "candidate_join_url": row["join_web_url"],
    }


async def get_ai_interview(ai_interview_id: UUID) -> dict | None:
    row = await ai_interview_repo.get_ai_interview(ai_interview_id)
    return dict(row) if row else None


async def reschedule_ai_interview(ai_interview_id: UUID, new_start: datetime, timezone_name: str | None = None) -> dict:
    row = await ai_interview_repo.get_ai_interview(ai_interview_id)
    if not row:
        raise AiInterviewNotFoundError(str(ai_interview_id))

    duration_minutes = row["duration_minutes"]
    new_end = new_start + timedelta(minutes=duration_minutes)

    # Move the Graph meeting's time window too, best-effort — a failure here
    # shouldn't block updating our own records, but is logged loudly.
    if row["meeting_id"]:
        try:
            await meeting_provider.update_meeting(
                row["meeting_id"], {"startDateTime": new_start.isoformat(), "endDateTime": new_end.isoformat()}
            )
        except Exception as e:
            logger.warning(f"Graph meeting reschedule failed (continuing anyway): {e}")

    from app.db.postgres import get_pool
    pool = await get_pool()
    await pool.execute(
        "UPDATE ai_interviews SET scheduled_start = $2, scheduled_end = $3, updated_at = now() WHERE id = $1",
        ai_interview_id, new_start, new_end,
    )

    if row["redis_interview_id"]:
        from app.scheduling.scheduler import SCHEDULE_KEY
        r = get_redis()
        await r.zrem(SCHEDULE_KEY, row["redis_interview_id"])
        await redis_schedule_interview(row["redis_interview_id"], new_start.timestamp())

    updated = await ai_interview_repo.get_ai_interview(ai_interview_id)
    return _to_schedule_response(updated)


async def cancel_ai_interview(ai_interview_id: UUID, reason: str) -> None:
    row = await ai_interview_repo.get_ai_interview(ai_interview_id)
    if not row:
        raise AiInterviewNotFoundError(str(ai_interview_id))

    if row["meeting_id"]:
        try:
            await meeting_provider.cancel_meeting(row["meeting_id"])
        except Exception as e:
            logger.warning(f"Graph meeting cancel failed (continuing anyway): {e}")

    if row["bot_account_reservation_id"]:
        await ai_interview_repo.release_reservation(row["bot_account_reservation_id"])

    if row["redis_interview_id"]:
        from app.scheduling.scheduler import SCHEDULE_KEY
        r = get_redis()
        await r.zrem(SCHEDULE_KEY, row["redis_interview_id"])
        try:
            await state_machine.transition(row["redis_interview_id"], InterviewState.CANCELLED, reason=reason)
        except Exception as e:
            logger.warning(f"Could not mark Redis state CANCELLED: {e}")

    await ai_interview_repo.cancel_ai_interview(ai_interview_id, reason)


async def start_ai_interview_dev(ai_interview_id: UUID, force_restart: bool = False) -> None:
    """Dev-only: force-dispatch right now, bypassing the normal
    schedule/pre-warm wait. Per the spec, this endpoint exists only for
    development testing — not meant for production scheduling paths.

    `force_restart=True` additionally lets you re-dispatch the *same*
    already-scheduled row over and over (e.g. repeatedly testing the bot
    join/speak flow against one reused meeting link, without re-running
    the whole schedule -> Graph/reservation pipeline each time): if the
    interview isn't still sitting in SCHEDULED, its Redis state is reset
    back to SCHEDULED (same meeting_url/join_mode/details) before
    dispatching, via the same `init_state` a brand-new interview uses --
    it fully overwrites worker_id/pid/error, so nothing from a previous
    attempt lingers into the new one."""
    row = await ai_interview_repo.get_ai_interview(ai_interview_id)
    if not row:
        raise AiInterviewNotFoundError(str(ai_interview_id))
    if not row["redis_interview_id"]:
        raise ValueError("This ai_interview has no associated Redis interview record to dispatch.")

    from app.scheduling.scheduler import SCHEDULE_KEY
    from app.scheduling.capacity import mark_active
    from app.worker.supervisor import start_worker

    r = get_redis()
    await r.zrem(SCHEDULE_KEY, row["redis_interview_id"])

    state = await state_machine.get_state(row["redis_interview_id"])
    if state is None:
        raise ValueError("No Redis state found for this interview.")

    if force_restart and state.get("state") != InterviewState.SCHEDULED.value:
        details = json.loads(state["details_json"])
        await state_machine.init_state(
            interview_id=row["redis_interview_id"],
            meeting_url=state["meeting_url"],
            join_mode=state["join_mode"],
            scheduled_start_epoch=float(state["scheduled_start_epoch"]),
            details=details,
        )
        await ai_interview_repo.update_ai_interview_status(ai_interview_id, InterviewState.SCHEDULED.value)
        state = await state_machine.get_state(row["redis_interview_id"])

    import json as _json
    details = _json.loads(state["details_json"])
    await mark_active(row["redis_interview_id"])
    await state_machine.transition(row["redis_interview_id"], InterviewState.PREPARING, reason="dev /start endpoint")
    await start_worker(row["redis_interview_id"], state["meeting_url"], state["join_mode"], details)
