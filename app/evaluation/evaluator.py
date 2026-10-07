import json
import logging
from pathlib import Path
from typing import List, Optional

from openai import AzureOpenAI
from pydantic import ValidationError

from app.config import get_settings
from app.api.evaluation_models import (
    LLMEvaluationOutput,
    EvaluationReport,
    JdFitScore,
    SkillScore,
)

logger = logging.getLogger(__name__)
settings = get_settings()

# NOTE: this must be a plain chat-completions deployment (e.g. gpt-4o), not the
# realtime voice deployment used in connection.py — they are different deployment
# types in Azure and the realtime one will not serve chat completions.
_client = AzureOpenAI(
    azure_endpoint=settings.azure_openai_chat_endpoint,
    api_key=settings.azure_openai_chat_key,
    api_version="2024-10-21",
)

PRIORITY_WEIGHTS = {"high": 2.0, "medium": 1.5, "low": 1.0}


def build_evaluator_prompt(transcript_data: dict) -> tuple[str, str]:
    position_title = transcript_data.get("position_title", "Unknown role")
    assessment_plan = transcript_data.get("assessment_plan") or {}
    job_description = transcript_data.get("job_description") or "Not provided"
    conversation_history = transcript_data.get("conversation_history", [])

    transcript_text = "\n".join(
        f"[{turn['speaker']}] {turn['text']}" for turn in conversation_history
    )

    skills_block = "\n".join(
        f"- {s['skill']} (priority: {s['priority']})"
        for s in assessment_plan.get("skills_to_test", [])
    ) or "No specific skill list was provided."

    system_prompt = f"""You are evaluating a technical interview transcript for the role of {position_title} ({assessment_plan.get('experience_level', 'unspecified')} level). You are given the assessment blueprint (what was planned to be tested) and the full transcript. Score strictly from evidence in the transcript, never infer a skill beyond what was actually said.

JOB DESCRIPTION
{job_description}

SKILLS PLANNED FOR ASSESSMENT
{skills_block}

SCORING RUBRIC for each skill (1-5)
1: No usable evidence. Could not answer, answer was off-topic, or the skill was never actually addressed even after a follow-up.
2: Attempted but vague. Named the right concepts but couldn't explain reasoning, collapsed under a follow-up.
3: Adequate. Correct fundamentals, but lacks depth, specificity, or real example detail.
4: Strong. Clear reasoning, references a specific real situation, handled follow-ups well.
5: Exceptional. Discusses tradeoffs and edge cases unprompted, judgment beyond what was asked.

CRITICAL RULES
- If a planned skill was never actually addressed in the transcript, set verification_status to "not_assessed" and leave score as null. Do not score it as a failure.
- Never penalize the candidate for the interviewer's phrasing, interruptions, or technical glitches visible in the transcript.
- Every rationale must reference something concretely said or notably absent, paraphrased in your own words, never quoted verbatim.
- Do not invent claims the candidate didn't make. If evidence is ambiguous, score conservatively and say why.
- red_flags should describe specific behavioral patterns, not just restate low scores. Use an empty array if there are none.
- Do not compute or include any weighted fit score, that is handled outside this step.

Respond with ONLY a JSON object in exactly this shape, no markdown fences, no commentary:
{{
  "skill_scores": [
    {{"skill": "string", "score": 1-5 or null, "verification_status": "verified_strong|verified_weak|not_assessed", "rationale": "string"}}
  ],
  "cross_cutting_scores": {{
    "communication_clarity": 1-5,
    "depth_vs_surface": 1-5,
    "problem_solving_approach": 1-5,
    "consistency_under_followup": 1-5
  }},
  "red_flags": ["string"],
  "recommendation": "strong_yes|yes|borderline|no",
  "summary": "2-3 factual sentences"
}}"""

    user_content = f"FULL TRANSCRIPT:\n{transcript_text}"
    return system_prompt, user_content


def _call_llm(system_prompt: str, user_content: str, max_retries: int = 1) -> LLMEvaluationOutput:
    last_error: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        response = _client.chat.completions.create(
            model=settings.azure_openai_chat_deployment_model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        raw_output = response.choices[0].message.content
        try:
            parsed = json.loads(raw_output)
            return LLMEvaluationOutput(**parsed)
        except (json.JSONDecodeError, ValidationError) as e:
            last_error = e
            logger.warning(f"Evaluator output failed validation on attempt {attempt + 1}: {e}")

    raise RuntimeError(f"Evaluator failed to produce valid output after {max_retries + 1} attempts: {last_error}")


def compute_jd_fit_score(skill_scores: List[SkillScore], assessment_plan: dict) -> JdFitScore:
    skill_priority_map = {
        s["skill"]: s.get("priority", "medium")
        for s in assessment_plan.get("skills_to_test", [])
    }

    weighted_sum = 0.0
    total_weight = 0.0
    skipped = []

    for s in skill_scores:
        if s.score is None:
            skipped.append(s.skill)
            continue
        weight = PRIORITY_WEIGHTS.get(skill_priority_map.get(s.skill, "medium"), 1.0)
        weighted_sum += s.score * weight
        total_weight += weight

    weighted_score = round(weighted_sum / total_weight, 2) if total_weight > 0 else 0.0

    note = "High priority skills weighted 2x, medium 1.5x, low 1x."
    if skipped:
        note += f" Excluded from score (not assessed): {', '.join(skipped)}."

    return JdFitScore(weighted_score=weighted_score, scale="1-5", weighting_note=note)


def evaluate_transcript(transcript_data: dict, interview_id: Optional[str] = None) -> EvaluationReport:
    system_prompt, user_content = build_evaluator_prompt(transcript_data)
    llm_output = _call_llm(system_prompt, user_content)

    assessment_plan = transcript_data.get("assessment_plan") or {}
    jd_fit_score = compute_jd_fit_score(llm_output.skill_scores, assessment_plan)

    candidate_id = transcript_data.get("candidate_name", "unknown")
    resolved_interview_id = interview_id or transcript_data.get("session_start", "unknown").replace(":", "-")

    return EvaluationReport(
        candidate_id=candidate_id,
        interview_id=resolved_interview_id,
        jd_fit_score=jd_fit_score,
        **llm_output.model_dump(),
    )