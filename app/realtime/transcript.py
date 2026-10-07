import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


class TranscriptSessionLogger:
    def __init__(
        self,
        candidate_name: str,
        position_title: str,
        assessment_plan: Optional[dict] = None,
        job_description: Optional[str] = None,
    ):
        self.candidate_name = candidate_name
        self.position_title = position_title
        self.assessment_plan = assessment_plan
        self.job_description = job_description
        self.turns = []
        self.start_time = datetime.now().isoformat()

    def log_turn(self, speaker: str, text: str):
        turn_data = {
            "timestamp": datetime.now().isoformat(),
            "speaker": speaker,
            "text": text
        }
        self.turns.append(turn_data)
        logger.info(f"[{speaker}] {text}")

    def build_payload(self) -> dict:
        """Assembles the same shape that used to be written to a local JSON
        file (see save_session_to_disk, removed) -- now handed to the caller
        to store in Postgres instead (ai_interview_sessions.full_transcript_json),
        since local disk doesn't survive a container restart/redeploy the
        way a database row does."""
        return {
            "candidate_name": self.candidate_name,
            "position_title": self.position_title,
            "assessment_plan": self.assessment_plan,
            "job_description": self.job_description,
            "session_start": self.start_time,
            "session_end": datetime.now().isoformat(),
            "conversation_history": self.turns,
        }