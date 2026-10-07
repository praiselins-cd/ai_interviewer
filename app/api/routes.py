import logging
import time
import uuid
from fastapi import APIRouter, HTTPException
from app.api.models import InterviewStartRequest
from app.state import machine as state_machine
from app.scheduling.scheduler import schedule_interview
# from app.api.evaluation_routes import router as evaluation_router
logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/v1/interviews/start")
async def start_interview(request: InterviewStartRequest):
    logger.info(f"Received meeting proxy initialization request for candidate: {request.candidate_name}")

    interview_id = uuid.uuid4().hex

    details = {
        "candidate": request.candidate_name,
        "title": request.position_title or request.assessment_plan.role,
        "assessment_plan": request.assessment_plan.model_dump(),
        "job_description": request.job_description,
        "duration_minutes": request.duration_minutes,
    }

    # Immediate-start callers (no scheduled_time) are scored "now", so
    # they're still capacity-gated but otherwise dispatch as soon as a slot
    # is free — the previous direct asyncio.create_task() call is replaced
    # by this persisted state + scheduler-driven dispatch so every interview
    # (scheduled or immediate) goes through the same worker-isolation,
    # locking, and capacity path.
    scheduled_start_epoch = request.scheduled_time.timestamp() if request.scheduled_time else time.time()

    await state_machine.init_state(
        interview_id=interview_id,
        meeting_url=request.meeting_url,
        join_mode=request.join_mode,
        scheduled_start_epoch=scheduled_start_epoch,
        details=details,
    )
    await schedule_interview(interview_id, scheduled_start_epoch)

    return {
        "status": "scheduled",
        "interview_id": interview_id,
        "message": "Interview accepted; a worker will be dispatched once capacity and pre-warm timing allow.",
        "candidate": request.candidate_name,
        "meeting_url": request.meeting_url,
        "join_mode": request.join_mode,
    }


@router.get("/v1/interviews/{interview_id}/status")
async def get_interview_status(interview_id: str):
    state = await state_machine.get_state(interview_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Unknown interview_id")
    return state
