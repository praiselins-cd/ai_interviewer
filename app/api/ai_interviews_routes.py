import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from datetime import datetime

from app.services import ai_interview_service as service
from app.state import machine as state_machine

logger = logging.getLogger("app.api.ai_interviews")
router = APIRouter(prefix="/v1/ai-interviews")


class ScheduleAiInterviewRequest(BaseModel):
    assessment_plan_id: UUID
    assessment_plan_version: int
    scheduled_start: datetime
    timezone: str
    # Testing-only escape hatch: lets you schedule with scheduled_start in
    # the past (or right now) so the scheduler's 3-minute pre-warm window
    # picks it up immediately, instead of waiting for a real future time.
    allow_past_start: bool = False
    # Testing-only escape hatch: skip Graph meeting creation and reuse an
    # already-existing Teams meeting link instead, so repeated join/admit
    # testing doesn't need a fresh meeting (and fresh Graph call) every time.
    existing_meeting_url: str | None = None


class RescheduleAiInterviewRequest(BaseModel):
    scheduled_start: datetime
    timezone: str | None = None


class CancelAiInterviewRequest(BaseModel):
    reason: str = "cancelled by caller"


@router.post("")
async def schedule_ai_interview(request: ScheduleAiInterviewRequest):
    try:
        result = await service.schedule_ai_interview(
            assessment_plan_id=request.assessment_plan_id,
            assessment_plan_version=request.assessment_plan_version,
            scheduled_start=request.scheduled_start,
            timezone_name=request.timezone,
            allow_past_start=request.allow_past_start,
            existing_meeting_url=request.existing_meeting_url,
        )
        return result
    except service.PastScheduleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except service.PlanNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except NotImplementedError as e:
        # load_assessment_plan() is still a stub — surfaced clearly rather
        # than as an opaque 500.
        raise HTTPException(status_code=501, detail=str(e))
    except Exception as e:
        from app.repositories.ai_interview_repo import BotAccountBusyError
        if isinstance(e, BotAccountBusyError):
            raise HTTPException(status_code=409, detail=str(e))
        logger.exception("Failed to schedule AI interview")
        raise HTTPException(status_code=500, detail="Failed to schedule interview")


@router.get("/{ai_interview_id}")
async def get_ai_interview(ai_interview_id: UUID):
    result = await service.get_ai_interview(ai_interview_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Unknown ai_interview_id")
    return result


@router.patch("/{ai_interview_id}/schedule")
async def reschedule_ai_interview(ai_interview_id: UUID, request: RescheduleAiInterviewRequest):
    try:
        return await service.reschedule_ai_interview(ai_interview_id, request.scheduled_start, request.timezone)
    except service.AiInterviewNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown ai_interview_id")


@router.post("/{ai_interview_id}/cancel")
async def cancel_ai_interview(ai_interview_id: UUID, request: CancelAiInterviewRequest):
    try:
        await service.cancel_ai_interview(ai_interview_id, request.reason)
        return {"status": "cancelled"}
    except service.AiInterviewNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown ai_interview_id")


@router.post("/{ai_interview_id}/start")
async def start_ai_interview_dev(ai_interview_id: UUID, force_restart: bool = False):
    """Development-only: force-dispatch the worker immediately, bypassing
    the normal schedule/pre-warm timing. Not for production use.

    `?force_restart=true` lets you re-dispatch the same already-scheduled
    interview repeatedly (e.g. testing bot join/speak against one reused
    meeting link) instead of getting the usual 409 for anything past
    SCHEDULED."""
    try:
        await service.start_ai_interview_dev(ai_interview_id, force_restart=force_restart)
        return {"status": "dispatched"}
    except service.AiInterviewNotFoundError:
        raise HTTPException(status_code=404, detail="Unknown ai_interview_id")
    except state_machine.IllegalTransitionError:
        raise HTTPException(
            status_code=409,
            detail=(
                "This interview has already reached a terminal state (FAILED/COMPLETED/etc) and "
                "cannot be restarted -- schedule a new interview for this assessment plan instead."
            ),
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
