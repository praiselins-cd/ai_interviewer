"""Thin asyncpg repository covering all 4 Module 2 tables (bot_account_reservations,
ai_interviews, ai_interview_sessions, interview_transcript_turns, interview_events —
kept in one file per the simplified plan, since they're small and used together
constantly). No ORM: plain parameterized SQL. The DDL these functions assume lives
in the plan doc (C:\\Users\\PraiselinSG\\.claude\\plans\\is-it-a-proper-spicy-melody.md)
and must be run against Postgres by hand before this module is used.
"""
import json
from datetime import datetime, timezone
from uuid import UUID

import asyncpg

from app.db.postgres import get_pool


class BotAccountBusyError(Exception):
    """Raised when the bot account already has an active RESERVED row
    (the partial unique index on bot_account_reservations enforces this at
    the DB level — this just translates that into a clean Python error)."""


# ---- bot_account_reservations ----------------------------------------------

async def reserve_bot_account(
    bot_account_email: str, ai_interview_id: UUID, reserved_from: datetime, reserved_until: datetime
) -> UUID:
    pool = await get_pool()
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO bot_account_reservations
                (bot_account_email, ai_interview_id, reserved_from, reserved_until)
            VALUES ($1, $2, $3, $4)
            RETURNING id
            """,
            bot_account_email, ai_interview_id, reserved_from, reserved_until,
        )
        return row["id"]
    except asyncpg.UniqueViolationError as e:
        raise BotAccountBusyError(f"Bot account {bot_account_email} already has an active reservation.") from e


async def release_reservation(reservation_id: UUID) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE bot_account_reservations SET status = 'RELEASED', released_at = now() WHERE id = $1",
        reservation_id,
    )


async def expire_stale_reservations() -> int:
    """Best-effort cleanup for reservations that ran past their window without
    being explicitly released (e.g. a worker crash). Safe to call periodically."""
    pool = await get_pool()
    result = await pool.execute(
        "UPDATE bot_account_reservations SET status = 'EXPIRED' "
        "WHERE status = 'RESERVED' AND reserved_until < now()"
    )
    return int(result.split()[-1]) if result else 0


# ---- ai_interviews -----------------------------------------------------------

async def create_ai_interview(
    assessment_plan_id: UUID,
    assessment_plan_version: int,
    assessment_plan_snapshot: dict,
    candidate_name: str,
    position_title: str | None,
    scheduled_start: datetime,
    scheduled_end: datetime,
    timezone_name: str,
    duration_minutes: int,
) -> UUID:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO ai_interviews
            (assessment_plan_id, assessment_plan_version, assessment_plan_snapshot,
             candidate_name, position_title, scheduled_start, scheduled_end, timezone, duration_minutes)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING id
        """,
        assessment_plan_id, assessment_plan_version, json.dumps(assessment_plan_snapshot),
        candidate_name, position_title, scheduled_start, scheduled_end, timezone_name, duration_minutes,
    )
    return row["id"]


async def get_ai_interview(ai_interview_id: UUID) -> asyncpg.Record | None:
    pool = await get_pool()
    return await pool.fetchrow("SELECT * FROM ai_interviews WHERE id = $1", ai_interview_id)


async def get_ai_interview_by_redis_id(redis_interview_id: str) -> asyncpg.Record | None:
    pool = await get_pool()
    return await pool.fetchrow("SELECT * FROM ai_interviews WHERE redis_interview_id = $1", redis_interview_id)


async def release_reservation_for_ai_interview(ai_interview_id: UUID) -> None:
    """Best-effort: releases whichever bot_account_reservation this
    ai_interview holds, if any -- a no-op if it never got one or it's
    already released. Called from every place a worker's run actually
    ends (success, crash, or orphan-detected-by-reconcile) so a dead
    interview doesn't permanently tie up the one bot account, which
    `expire_stale_reservations` alone can't fully cover (a reservation's
    `reserved_until` window can still be in the future relative to "now"
    even though the interview it belongs to has already finished)."""
    row = await get_ai_interview(ai_interview_id)
    if row and row["bot_account_reservation_id"]:
        await release_reservation(row["bot_account_reservation_id"])


async def update_ai_interview_status(ai_interview_id: UUID, status: str) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE ai_interviews SET status = $2, updated_at = now() WHERE id = $1", ai_interview_id, status
    )


async def set_meeting_details(ai_interview_id: UUID, meeting_id: str, join_web_url: str) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE ai_interviews SET meeting_id = $2, join_web_url = $3, updated_at = now() WHERE id = $1",
        ai_interview_id, meeting_id, join_web_url,
    )


async def set_bot_account_reservation(ai_interview_id: UUID, reservation_id: UUID) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE ai_interviews SET bot_account_reservation_id = $2, updated_at = now() WHERE id = $1",
        ai_interview_id, reservation_id,
    )


async def set_redis_interview_id(ai_interview_id: UUID, redis_interview_id: str) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE ai_interviews SET redis_interview_id = $2, updated_at = now() WHERE id = $1",
        ai_interview_id, redis_interview_id,
    )


async def cancel_ai_interview(ai_interview_id: UUID, reason: str) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE ai_interviews
        SET status = 'CANCELLED', cancelled_at = now(), cancel_reason = $2, updated_at = now()
        WHERE id = $1
        """,
        ai_interview_id, reason,
    )


async def find_scheduled_by_plan(assessment_plan_id: UUID, assessment_plan_version: int) -> asyncpg.Record | None:
    """Used for idempotent scheduling: a plan id+version that already has a
    non-terminal ai_interviews row shouldn't create a duplicate meeting."""
    pool = await get_pool()
    return await pool.fetchrow(
        """
        SELECT * FROM ai_interviews
        WHERE assessment_plan_id = $1 AND assessment_plan_version = $2
          AND status NOT IN ('COMPLETED','CANCELLED','FAILED','MEETING_CREATION_FAILED',
                              'BOT_JOIN_FAILED','CANDIDATE_NO_SHOW','TECHNICAL_FAILURE')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        assessment_plan_id, assessment_plan_version,
    )


# ---- ai_interview_sessions ----------------------------------------------------

async def create_session(ai_interview_id: UUID, session_number: int, redis_interview_id: str | None = None) -> UUID:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO ai_interview_sessions (ai_interview_id, session_number, status, redis_interview_id)
        VALUES ($1, $2, 'SCHEDULED', $3)
        RETURNING id
        """,
        ai_interview_id, session_number, redis_interview_id,
    )
    return row["id"]


async def get_session(session_id: UUID) -> asyncpg.Record | None:
    pool = await get_pool()
    return await pool.fetchrow("SELECT * FROM ai_interview_sessions WHERE id = $1", session_id)


async def get_latest_session(ai_interview_id: UUID) -> asyncpg.Record | None:
    pool = await get_pool()
    return await pool.fetchrow(
        "SELECT * FROM ai_interview_sessions WHERE ai_interview_id = $1 ORDER BY session_number DESC LIMIT 1",
        ai_interview_id,
    )


async def update_session_status(session_id: UUID, status: str) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE ai_interview_sessions SET status = $2 WHERE id = $1", session_id, status
    )


async def mark_session_started(session_id: UUID) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE ai_interview_sessions SET started_at = now() WHERE id = $1", session_id
    )


async def mark_session_ended(session_id: UUID) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE ai_interview_sessions SET ended_at = now() WHERE id = $1", session_id
    )


async def save_full_transcript(session_id: UUID, transcript: dict) -> None:
    """Stores the whole assembled transcript (see
    TranscriptSessionLogger.build_payload) on the session row -- replaces
    the old local-disk JSON file, which doesn't survive a container
    restart/redeploy. The per-line interview_transcript_turns table remains
    the real-time source of truth (what Module 3 reads); this is a
    convenience copy for "fetch the whole conversation in one query"."""
    pool = await get_pool()
    await pool.execute(
        "UPDATE ai_interview_sessions SET full_transcript_json = $2 WHERE id = $1",
        session_id, json.dumps(transcript),
    )


async def record_audio_check_attempt(session_id: UUID, passed: bool | None) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE ai_interview_sessions
        SET audio_check_attempts = audio_check_attempts + 1, audio_check_passed = $2
        WHERE id = $1
        """,
        session_id, passed,
    )


async def increment_questions_completed(session_id: UUID) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE ai_interview_sessions SET questions_completed = questions_completed + 1 "
        "WHERE id = $1",
        session_id,
    )


async def set_technical_failure(session_id: UUID, reason: str, retry_eligible: bool) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        UPDATE ai_interview_sessions
        SET technical_failure_reason = $2, retry_eligible = $3
        WHERE id = $1
        """,
        session_id, reason, retry_eligible,
    )


async def finalise_session(session_id: UUID) -> bool:
    """Idempotent: returns True if this call actually finalised the session,
    False if it was already finalised (no-op) — callers should treat False
    as success too, just "nothing to do"."""
    pool = await get_pool()
    result = await pool.execute(
        "UPDATE ai_interview_sessions SET finalised_at = now() "
        "WHERE id = $1 AND finalised_at IS NULL",
        session_id,
    )
    return result.endswith("1")


# ---- interview_transcript_turns ----------------------------------------------

async def next_sequence_number(session_id: UUID) -> int:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS next FROM interview_transcript_turns "
        "WHERE ai_interview_session_id = $1",
        session_id,
    )
    return row["next"]


async def insert_transcript_turn(
    session_id: UUID,
    speaker: str,
    text: str,
    question_id: str | None = None,
    started_at: datetime | None = None,
    is_final: bool = True,
    transcription_confidence: float | None = None,
) -> UUID:
    pool = await get_pool()
    seq = await next_sequence_number(session_id)
    row = await pool.fetchrow(
        """
        INSERT INTO interview_transcript_turns
            (ai_interview_session_id, question_id, speaker, sequence_number, started_at,
             text, is_final, transcription_confidence)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id
        """,
        session_id, question_id, speaker, seq, started_at or datetime.now(timezone.utc),
        text, is_final, transcription_confidence,
    )
    return row["id"]


async def list_transcript_turns(session_id: UUID) -> list[asyncpg.Record]:
    pool = await get_pool()
    return await pool.fetch(
        "SELECT * FROM interview_transcript_turns WHERE ai_interview_session_id = $1 ORDER BY sequence_number",
        session_id,
    )


# ---- interview_events ----------------------------------------------------------

async def insert_event(session_id: UUID, event_type: str, event_data: dict | None = None) -> None:
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO interview_events (ai_interview_session_id, event_type, event_data) VALUES ($1, $2, $3)",
        session_id, event_type, json.dumps(event_data) if event_data is not None else None,
    )


async def list_events(session_id: UUID) -> list[asyncpg.Record]:
    pool = await get_pool()
    return await pool.fetch(
        "SELECT * FROM interview_events WHERE ai_interview_session_id = $1 ORDER BY occurred_at", session_id
    )
