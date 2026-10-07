import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.api.evaluation_models import EvaluateRequest, EvaluationReport
from app.evaluation.evaluator import evaluate_transcript

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/v1/interviews/evaluate", response_model=EvaluationReport)
async def evaluate_interview(request: EvaluateRequest):
    interview_id = None

    if request.transcript_payload:
        transcript_data = request.transcript_payload
    elif request.transcript_path:
        path = Path(request.transcript_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"Transcript file not found: {path}")
        transcript_data = json.loads(path.read_text(encoding="utf-8"))
        interview_id = path.stem
    else:
        raise HTTPException(status_code=400, detail="Provide either transcript_path or transcript_payload")

    try:
        report = evaluate_transcript(transcript_data, interview_id=interview_id)
    except Exception as e:
        logger.error(f"Evaluation failed: {e}")
        raise HTTPException(status_code=502, detail="Evaluator LLM call failed or returned invalid output")

    output_dir = Path("data/evaluations")
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"evaluation_{report.candidate_id.replace(' ', '_')}_{report.interview_id}.json"
    out_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    return report