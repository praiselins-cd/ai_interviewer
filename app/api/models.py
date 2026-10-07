from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime


class SkillPriority(BaseModel):
    skill: str
    priority: str  # "high" | "medium" | "low"


class SkillAssessmentPlan(BaseModel):
    role: str
    experience_level: str
    difficulty: str
    num_questions: int
    skills_to_test: List[SkillPriority]


# model.py
class InterviewStartRequest(BaseModel):
    meeting_url: str = Field(..., description="The complete Microsoft Teams meeting invitation link")
    candidate_name: str = Field("Candidate", description="The name of the individual being interviewed")
    position_title: Optional[str] = Field(None, description="Falls back to assessment_plan.role if not provided")
    job_description: Optional[str] = Field(None, description="Stored alongside the transcript for the evaluator stage")
    resume_text: Optional[str] = Field(None, description="Stored alongside the transcript for the evaluator stage")
    duration_minutes: int = Field(30, description="Scheduled interview length in minutes, drives the wrap-up timer")
    join_mode: str = Field(
        "signed_in",
        description="'signed_in' reuses the persistent authenticated bot profile (fast lobby admission, "
        "but only one meeting at a time per account). 'guest' joins anonymously from a fresh throwaway "
        "profile and is also admitted by a brief visit from the signed-in account, letting multiple "
        "interviews run concurrently. 'guest_only' joins anonymously with no signed-in admitter at all — "
        "it just waits in the lobby until a real human participant, or the tenant's auto-admit policy, "
        "lets it in.",
    )
    scheduled_time: Optional[datetime] = Field(
        None,
        description="When the interview should actually start. Omit to dispatch as soon as capacity allows "
        "(current behavior); provide a future timestamp to have the worker pre-warmed shortly beforehand "
        "instead of sitting in a normal queue.",
    )
    assessment_plan: SkillAssessmentPlan
