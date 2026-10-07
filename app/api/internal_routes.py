import json
import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException

from app.repositories import ai_interview_repo
from app.infra.redis_client import get_redis

logger = logging.getLogger("app.api.internal")
router = APIRouter(prefix="/internal/v1")

FINALISED_CHANNEL = "interview_session_finalised"


@router.post("/interview-sessions/{session_id}/finalise")
async def finalise_interview_session(session_id: UUID):
    session = await ai_interview_repo.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown interview_session_id")

    if session["finalised_at"] is not None:
        # Idempotent: already finalised, no-op, no re-publish.
        return {"status": "already_finalised", "interview_session_id": str(session_id)}

    did_finalise = await ai_interview_repo.finalise_session(session_id)
    if not did_finalise:
        # Lost a race with another finalise call — treat as success, don't double-publish.
        return {"status": "already_finalised", "interview_session_id": str(session_id)}

    r = get_redis()
    await r.publish(FINALISED_CHANNEL, json.dumps({"interview_session_id": str(session_id)}))
    logger.info(f"Published {FINALISED_CHANNEL} for session {session_id}")

    return {"status": "finalised", "interview_session_id": str(session_id)}
