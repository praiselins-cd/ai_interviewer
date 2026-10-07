from pydantic import BaseModel
from typing import List, Optional, Literal


class SkillScore(BaseModel):
    skill: str
    score: Optional[int] = None  # 1-5, null if the skill was never actually addressed
    verification_status: Literal["verified_strong", "verified_weak", "not_assessed"]
    rationale: str


class CrossCuttingScores(BaseModel):
    communication_clarity: int
    depth_vs_surface: int
    problem_solving_approach: int
    consistency_under_followup: int


class JdFitScore(BaseModel):
    weighted_score: float
    scale: str = "1-5"
    weighting_note: str


class LLMEvaluationOutput(BaseModel):
    """What we actually ask the model to produce. No ids, no weighted math."""
    skill_scores: List[SkillScore]
    cross_cutting_scores: CrossCuttingScores
    red_flags: List[str]
    recommendation: Literal["strong_yes", "yes", "borderline", "no"]
    summary: str


class EvaluationReport(LLMEvaluationOutput):
    """The full report returned to the caller, ids and jd_fit_score added in code."""
    candidate_id: str
    interview_id: str
    jd_fit_score: JdFitScore


class EvaluateRequest(BaseModel):
    transcript_path: Optional[str] = None
    transcript_payload: Optional[dict] = None