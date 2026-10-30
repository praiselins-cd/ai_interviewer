import asyncio
import base64
import json
import logging
import time
import uuid

from app.config import get_settings
from app.realtime.concepts import split_expected_concepts
from app.realtime.transcript import TranscriptSessionLogger

from aws_sdk_bedrock_runtime.client import AsyncBedrockRuntimeClient, InvokeModelWithBidirectionalStreamOperationInput
from aws_sdk_bedrock_runtime.models import InvokeModelWithBidirectionalStreamInputChunk, BidirectionalInputPayloadPart
from aws_sdk_bedrock_runtime.config import AsyncBedrockRuntimeConfig
from smithy_http.aio.crt import AWSCRTHTTPClient
from smithy_aws_core.identity.environment import EnvironmentCredentialsResolver

logger = logging.getLogger(__name__)
settings = get_settings()


# ── Constants ─────────────────────────────────────────────────────────────────

TIME_UPDATE_INTERVAL_SECONDS = 120
WRAP_UP_THRESHOLD_SECONDS = 300
HARD_CUTOFF_GRACE_SECONDS = 60
MAX_AUDIO_CHECK_ATTEMPTS = 4
SILENCE_REPROMPT_SECONDS = 30
AUDIO_CHECK_FAILURE_GRACE_SECONDS = 60
TIME_UP_CANDIDATE_WAIT_CAP_SECONDS = 90
# Nova Sonic bidirectional-stream connections are capped at ~8 minutes by
# AWS. Rotate to a fresh connection a minute early so a rotation is never
# racing the hard cutoff, carrying forward a short progress summary (not the
# full raw transcript -- AWS caps carried-forward context at 50KB/message,
# 200KB total, and a short summary is both simpler and safely under that).
SESSION_ROTATE_AFTER_SECONDS = 7 * 60
SESSION_ROTATE_FORCE_AFTER_SECONDS = 7 * 60 + 45
# Chat history replayed into a rotated session. Nova Sonic caps the size of
# history it accepts, so each message and the total are kept conservative.
HISTORY_MESSAGE_MAX_CHARS = 1000
HISTORY_TOTAL_MAX_CHARS = 30000
# Nova Sonic's confirmed input format (per AWS's own sample code); output
# stays 24kHz PCM like the previous Azure setup, so browser playback is
# unchanged -- only the capture-side AudioContext sample rate needed to
# change (see custom_audio_payload.js) to avoid a manual resampling step.
AUDIO_INPUT_SAMPLE_RATE = 16000
AUDIO_OUTPUT_SAMPLE_RATE = 24000


# ── Prompt builders ───────────────────────────────────────────────────────────

def render_time_status(remaining_seconds: float) -> str:
    minutes_remaining = max(0, int(remaining_seconds // 60))
    if remaining_seconds <= 0:
        return (
            "\n\nTIME STATUS: The scheduled interview time has ended. Do not ask any further questions. "
            "If the candidate is still mid-answer, let them finish completely without interrupting -- "
            "only once they're done (or there's a natural pause), give a brief closing statement, then "
            "call the end_call tool right now."
        )
    elif remaining_seconds <= WRAP_UP_THRESHOLD_SECONDS:
        return (
            f"\n\nTIME STATUS: About {minutes_remaining} minutes remaining. Finish the current topic, "
            "ask at most one more question if a high priority skill hasn't been covered yet, then begin "
            "moving toward your closing remarks."
        )
    else:
        return f"\n\nTIME STATUS: {minutes_remaining} minutes remaining in the interview. Continue at current pace."


def build_audio_check_prompt(details: dict) -> str:
    candidate = details.get("candidate", "Candidate")
    return f"""
You are an AI interviewer named Talvy, about to begin a voice interview with {candidate}. Right now, ONLY do the following:
1. Greet {candidate} briefly and warmly.
2. Ask: "Before we begin, can you hear me clearly?"
3. Listen to their response.
4. Call the report_audio_check tool: candidate_confirmed=true if they clearly confirm they can hear you (e.g. "yes", "I can hear you"), candidate_confirmed=false if they say they can't hear you, sound confused about being asked, or don't give a clear answer.
Tool calls are silent internal actions. Never say or read aloud tool names, arguments, IDs, or status values. Do not announce a tool call.
Do not ask any interview questions yet, and do not mention scoring, competencies, or the interview structure.
"""


def build_audio_retry_prompt(details: dict) -> str:
    candidate = details.get("candidate", "Candidate")
    return f"""
{candidate} indicated they could not hear you clearly. Say a short test phrase, for example "Can you hear this test message clearly now?", then wait for their response and silently call report_audio_check with the result. This is your last retry — if they still can't hear you, tell them there seems to be a technical issue on the connection, apologize briefly, and silently call end_call. Never speak or read aloud tool names, arguments, IDs, or status values.
"""


def build_audio_failure_prompt(details: dict) -> str:
    candidate = details.get("candidate", "Candidate")
    return f"""
The audio check with {candidate} has failed twice. Apologize briefly, explain there appears to be a technical issue preventing the interview from continuing, and that the team will follow up to reschedule. Then silently call the end_call tool immediately. Tool calls are internal actions: never speak or read aloud tool names, arguments, IDs, or status values. Do not ask any interview questions.
"""


def _render_question(q: dict) -> str:
    """One question for the prompt. New-format plans list their expected
    answer as numbered concept clauses (the units the evaluator scores);
    plans with fewer than 2 clauses predate that format and keep the old
    free-text baseline."""
    line = f'- {q.get("question_id")}: {q.get("question")}'
    concepts = split_expected_concepts(q.get("expected_answer"))
    if len(concepts) >= 2:
        line += "\n  Required concepts:" + "".join(f"\n    {i}. {c}" for i, c in enumerate(concepts, 1))
    else:
        line += f' (legacy)\n  Baseline concepts (minimum bar): {q.get("expected_answer", "")}'
    if q.get("target_seconds"):
        line += f'\n  Time budget: ~{q["target_seconds"]}s'
    return line


def build_interview_prompt(details: dict) -> str:
    """Builds the system prompt for the main interview phase using the frozen
    assessment-plan snapshot: approved questions, competencies, and candidate
    context. Never includes the full resume or job description."""
    snapshot = details.get("assessment_plan_snapshot") or {}
    candidate = details.get("candidate", "Candidate")
    title = details.get("position_title") or snapshot.get("role", "the role")
    duration_minutes = details.get("duration_minutes", 15)
    questions = snapshot.get("questions", [])  # [{"question_id","priority","question","competency_id","expected_answer","evidence","target_seconds"}, ...]

    # Sorted by priority so the model's required question order actually
    # matches the "ask in order of priority" instruction below.
    sorted_questions = sorted(questions, key=lambda q: q.get("priority", 999))
    num_questions = len(sorted_questions)

    questions_block = "\n".join(_render_question(q) for q in sorted_questions) or "No approved questions were provided."

    return f"""
# Role
You are an AI interviewer named Talvy, continuing a {duration_minutes}-minute live voice interview for {title} with candidate {candidate}. The audio check has already passed — begin the actual interview now.

## VOICE OUTPUT RULES (critical, this is spoken, not written)
- Tool calls are silent internal actions, never spoken dialogue. Never say or read aloud a tool name (including question_started, question_completed, or end_call), any tool arguments, question IDs, booleans, or status values. Do not announce that you are calling a tool; speak only natural interview dialogue.
- In your very first turn: briefly introduce yourself (e.g. "Hi {candidate}, I'm Talvy, and I'll be conducting your interview today"), politely remind the candidate to turn on their camera if it's off, and immediately ask the first question ({sorted_questions[0].get("question_id") if sorted_questions else "the first question"}) in that SAME TURN. CRITICAL: Do NOT pause or wait for an acknowledgment before asking the question. Speak the introduction, camera request, and first question together in ONE continuous turn.
- Speak in natural friendly, happy, respectful emotional engagement.
- Be patient: candidates frequently pause for 3-5 seconds to think, reflect, or structure their answers mid-sentence. Never cut them off or jump in while they are thinking. Only consider their answer complete when they have clearly finished speaking their thought.
- Ask ONE question at a time. Keep turns short: 1-3 sentences unless transitioning.
- Acknowledge the candidate's answer briefly before moving on, but do not repeat their answer back to them. For example, say "Thank you for your answer" or "I appreciate your response" before asking the next question.
- Use natural acknowledgments before moving on, don't be robotic.
- When moving to the next question, say the transition phrase and the next question together in the SAME turn — NEVER end a turn on a transition sentence alone (e.g. "I will now start with the next question" or "Are you ready?") and wait for the candidate to respond to it. Always state the transition and ask the question immediately.
- Never reveal you are scoring or evaluating specific competencies.
- You are not a hiring manager — no promises about outcomes, salary, or timelines.
- Ensure you ask all required questions in order of priority.
- Silently invoke question_started with the question's ID as you begin asking one of the {num_questions or "approved"} questions above.
- When concluding the interview, when time has ended, or if the candidate asks to end early: ALWAYS speak your complete closing statement to the candidate FIRST (thank them, provide warm closing remarks). Inform the candidate that they can leave the meeting and it will take a minute for you to leave. Silently invoke end_call only AFTER you have finished speaking your closing remarks.
- Don't coach the candidate or hint at what you're looking for.
- Don't discuss compensation, visas, or other HR/legal topics.

## ASKING THE QUESTION
- Ask each question exactly as written. Do not add hints, examples, or a rephrasing with clues before the candidate has answered once. If the candidate asks you to repeat it, repeat it as written.
- Do not speak while the candidate is answering — no "mm-hm", no acknowledgements, no nudges mid-answer. Wait until they have clearly finished.
- If the candidate goes off-topic, asks a meta-question ("did I answer correctly?"), or only says a filler ("okay", "sure") — do NOT call question_completed. Respond naturally, repeat the question as written, and wait for a genuine answer.

## FOLLOW-UP AND COMPLETION RULES
- Each question lists numbered "Required concepts". After the candidate's first answer, check each concept: was it clearly stated?
- For every concept NOT yet covered, ask ONE short, neutral follow-up that points toward that concept without stating it. Ask them one at a time, in order, and wait for each answer. Good: "What does the server do with that key?" Bad: "So the server stores the key with the result, right?"
- Ask at most one follow-up per missed concept. Never follow up on anything outside the listed concepts, and never ask a second follow-up for the same concept.
- Never confirm, correct, or praise the content of an answer ("right", "exactly", "not quite", "good point"). Use only neutral acknowledgements like "Thank you" or "Okay".
- "I don't know" is a valid answer: acknowledge it neutrally and move to the next missed concept, or to the next question.
- If the question's time budget is used up, skip the remaining follow-ups. Never combine several follow-ups into one question.
- Follow-ups are part of the same question: do NOT call question_started or question_completed for them.
- Call question_completed once you are done with the question — after the answer to the last follow-up, or right after the first answer if every concept was covered — with follow_up_count set to the number of follow-ups you asked (and follow_up_used true if it is above 0). Then move to the next question in the same turn.
- Questions marked "(legacy)" list "Baseline concepts" instead of numbered concepts: for those, ask at most ONE neutral follow-up about what was missing, then call question_completed.

## Required QUESTIONS (ask in this order)
{questions_block}
"""


# ── Tool schemas ──────────────────────────────────────────────────────────────

END_CALL_TOOL_SCHEMA = {
    "toolSpec": {
        "name": "end_call",
        "description": (
            "Ends the Teams meeting call. Call this only after delivering your closing remarks to the "
            "candidate, or immediately when a TIME STATUS update says the interview time has ended."
        ),
        "inputSchema": {"json": json.dumps({
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Brief reason, e.g. 'interview complete' or 'time limit reached'"}
            },
            "required": ["reason"],
        })},
    }
}

REPORT_AUDIO_CHECK_TOOL_SCHEMA = {
    "toolSpec": {
        "name": "report_audio_check",
        "description": "Report whether the candidate confirmed they can hear you clearly during the audio check.",
        "inputSchema": {"json": json.dumps({
            "type": "object",
            "properties": {
                "candidate_confirmed": {
                    "type": "boolean",
                    "description": "true if the candidate clearly confirmed they can hear you, false otherwise",
                }
            },
            "required": ["candidate_confirmed"],
        })},
    }
}

QUESTION_STARTED_TOOL_SCHEMA = {
    "toolSpec": {
        "name": "question_started",
        "description": "Call the instant you begin asking one of the approved main questions (not for follow-ups).",
        "inputSchema": {"json": json.dumps({
            "type": "object",
            "properties": {"question_id": {"type": "string", "description": "the question's id, e.g. 1, 2, 3"}},
            "required": ["question_id"],
        })},
    }
}

QUESTION_COMPLETED_TOOL_SCHEMA = {
    "toolSpec": {
        "name": "question_completed",
        "description": "Call once you are done with a question (after its follow-ups, if any) and moving on.",
        "inputSchema": {"json": json.dumps({
            "type": "object",
            "properties": {
                "question_id": {"type": "string"},
                "follow_up_used": {"type": "boolean"},
                "follow_up_count": {"type": "integer", "description": "number of follow-ups asked for this question"},
            },
            "required": ["question_id", "follow_up_used"],
        })},
    }
}


# ── Module helpers ─────────────────────────────────────────────────────────


def _fire_and_forget(coro, label: str = ""):
    """Schedules a coroutine as a background task, logging any exception
    rather than letting it silently vanish."""
    async def _run():
        try:
            await coro
        except Exception as e:
            logger.warning(f"Background task '{label}' failed: {e}")
    asyncio.create_task(_run())


async def _build_bedrock_client() -> AsyncBedrockRuntimeClient:
    import os
    # pydantic-settings loads .env into the Settings object but does NOT write
    # back to os.environ. EnvironmentCredentialsResolver reads os.environ
    # directly, so bridge the gap here for any var the .env supplied.
    for env_key, setting_val in (
        ("AWS_ACCESS_KEY_ID", settings.aws_access_key_id),
        ("AWS_SECRET_ACCESS_KEY", settings.aws_secret_access_key),
        ("AWS_SESSION_TOKEN", settings.aws_session_token),
    ):
        if setting_val and not os.environ.get(env_key):
            os.environ[env_key] = setting_val

    config = await AsyncBedrockRuntimeConfig.resolve(
        endpoint_uri=f"https://bedrock-runtime.{settings.aws_region}.amazonaws.com",
        region=settings.aws_region,
        aws_credentials_identity_resolver=EnvironmentCredentialsResolver(),
        transport=AWSCRTHTTPClient(),
    )
    return AsyncBedrockRuntimeClient(config=config)


# ── VoiceBridgeSession ────────────────────────────────────────────────────────

class VoiceBridgeSession:
    """Encapsulates all state and logic for one bot-to-browser WebSocket
    session bridged to an AWS Bedrock Nova Sonic bidirectional stream."""

    def __init__(self, websocket, details: dict, end_call_callback=None):
        self.websocket = websocket
        self.details = details
        self.end_call_callback = end_call_callback

        # Mode flags
        self.module2_mode = bool(details.get("assessment_plan_snapshot"))
        self.redis_interview_id = details.get("interview_id")
        self.pg_session_id = details.get("pg_session_id")

        # Lazy-loaded in run() when module2_mode is active
        self.state_machine = None
        self.ai_interview_repo = None

        # Bedrock stream state — swapped atomically during session rotation
        self.stream_response = None
        self.prompt_name: str | None = None
        self.audio_content_name: str | None = None
        self.bedrock_stream_lock = asyncio.Lock()
        self.bedrock_client: AsyncBedrockRuntimeClient | None = None

        # Interview flow state
        self.ai_responding = False
        self.greeted = False
        self.candidate_video_on = False
        self.waiting_since: float | None = None
        self.silence_nudge_sent = False
        self.candidate_speaking = False
        self.audio_playback_end_time = 0.0
        self.session_started_at = time.monotonic()
        self.audio_check_attempts = 0
        self.audio_check_passed = False
        self.current_question_id: str | None = None
        self.completed_question_ids: list[str] = []
        self.question_started_at: float | None = None
        self.question_target_seconds: float | None = None
        self.question_budget_warned = False
        # Sentences of the AI turn in progress; persisted as one transcript
        # turn when the turn ends, by which point question_started for that
        # turn has arrived and current_question_id is correct.
        self.ai_turn_buffer: list[str] = []
        # Disabled for the rest of the call if Bedrock rejects replayed
        # history, so reconnects fall back to the progress note alone.
        self.replay_history = True
        self.stream_has_history = False
        self.closing_spoken = False
        # Usage/latency for the ai_interviewer_llm_usage row. Token totals are
        # summed from usageEvent deltas, so they span every rotation/reconnect
        # (each connection's own running total restarts at 0).
        self.call_started_at = time.monotonic()
        self.usage_totals = {"input_speech": 0, "input_text": 0, "output_speech": 0, "output_text": 0}
        self.usage_event_logged = False
        self.connection_count = 0
        self.response_latencies_ms: list[int] = []
        self.latency_started_at: float | None = None
        self.end_call_event = asyncio.Event()
        self.interview_started_event = asyncio.Event()
        if not self.module2_mode:
            self.interview_started_event.set()  # legacy: timer starts immediately

        # Transcript logger
        self.session_logger = TranscriptSessionLogger(
            candidate_name=details.get("candidate", "Unknown"),
            position_title=details.get("title") or details.get("position_title", "Technical Role"),
            assessment_plan=details.get("assessment_plan") or details.get("assessment_plan_snapshot"),
            job_description=details.get("job_description"),
        )

        # Tool list
        self.tools = [END_CALL_TOOL_SCHEMA]
        if self.module2_mode:
            self.tools += [
                REPORT_AUDIO_CHECK_TOOL_SCHEMA,
                QUESTION_STARTED_TOOL_SCHEMA,
                QUESTION_COMPLETED_TOOL_SCHEMA,
            ]

    # ── State machine helpers ─────────────────────────────────────────────────

    async def _transition(self, to_state: str, reason: str):
        if not (self.module2_mode and self.redis_interview_id and self.state_machine):
            return
        try:
            await self.state_machine.transition(
                self.redis_interview_id,
                self.state_machine.InterviewState(to_state),
                reason=reason,
            )
        except Exception as e:
            logger.warning(f"State transition to {to_state} failed: {e}")

    def _flush_ai_turn(self):
        """Persists the buffered AI sentences as a single interviewer turn."""
        text = " ".join(self.ai_turn_buffer).strip()
        self.ai_turn_buffer = []
        if text and self.module2_mode and self.pg_session_id and self.ai_interview_repo:
            _fire_and_forget(
                self.ai_interview_repo.insert_transcript_turn(
                    self.pg_session_id, "AI_INTERVIEWER", text,
                    question_id=self.current_question_id,
                ),
                "transcript:ai",
            )

    def _mark_question_technical_issue(self, reason: str):
        """Flags the in-progress question as unassessable so the evaluator
        returns null for it instead of scoring a cut-off answer."""
        if self.current_question_id:
            logger.warning("Question %s marked as technical issue: %s", self.current_question_id, reason)
            self._log_event("QUESTION_TECHNICAL_ISSUE", {"question_id": self.current_question_id, "reason": reason})

    def _log_event(self, event_type: str, event_data: dict | None = None):
        if self.module2_mode and self.pg_session_id and self.ai_interview_repo:
            _fire_and_forget(
                self.ai_interview_repo.insert_event(self.pg_session_id, event_type, event_data),
                event_type,
            )

    def _record_llm_usage(self):
        """Writes this session's one ai_interviewer_llm_usage row. Cost is
        computed from the speech/text split before it's summed, since the two
        are priced differently."""
        t = self.usage_totals
        if sum(t.values()) == 0:
            logger.info("No Nova Sonic usage recorded for this session; skipping llm usage row.")
            return
        cost_usd = round((
            t["input_speech"] * settings.nova_sonic_speech_in_per_m
            + t["output_speech"] * settings.nova_sonic_speech_out_per_m
            + t["input_text"] * settings.nova_sonic_text_in_per_m
            + t["output_text"] * settings.nova_sonic_text_out_per_m
        ) / 1_000_000, 6)
        latencies = self.response_latencies_ms
        avg_latency = int(sum(latencies) / len(latencies)) if latencies else None
        max_latency = max(latencies) if latencies else None
        duration_ms = int((time.monotonic() - self.call_started_at) * 1000)
        logger.info(
            "Nova Sonic usage: speech in=%d out=%d, text in=%d out=%d, cost=$%.6f, "
            "connections=%d, latency avg=%s ms max=%s ms.",
            t["input_speech"], t["output_speech"], t["input_text"], t["output_text"], cost_usd,
            self.connection_count, avg_latency, max_latency,
        )
        session_id = uuid.UUID(str(self.pg_session_id))
        plan_id = self.details.get("assessment_plan_id")
        _fire_and_forget(
            self.ai_interview_repo.insert_llm_usage(
                interview_session_id=session_id,
                service="interview",
                call_label="nova_sonic_session",
                entity_id=session_id,
                model_name=settings.bedrock_model_id,
                input_token_count=t["input_speech"] + t["input_text"],
                output_token_count=t["output_speech"] + t["output_text"],
                input_speech_token_count=t["input_speech"],
                input_text_token_count=t["input_text"],
                output_speech_token_count=t["output_speech"],
                output_text_token_count=t["output_text"],
                cost_usd=cost_usd,
                duration_ms=duration_ms,
                avg_response_latency_ms=avg_latency,
                max_response_latency_ms=max_latency,
                connection_count=self.connection_count,
                assessment_plan_id=uuid.UUID(plan_id) if plan_id else None,
            ),
            "llm_usage",
        )

    # ── Bedrock event senders ─────────────────────────────────────────────────

    async def _send_bedrock_event(self, event: dict):
        if not self.stream_response:
            return
        payload = json.dumps(event).encode("utf-8")
        chunk = InvokeModelWithBidirectionalStreamInputChunk(value=BidirectionalInputPayloadPart(bytes_=payload))
        try:
            await self.stream_response.input_stream.send(chunk)
        except Exception as e:
            logger.warning(f"Failed to send event to Bedrock stream: {e}")

    async def _send_system_prompt(self, text: str):
        """Sends a SYSTEM-role text content block. Nova Sonic allows only ONE
        SYSTEM block per prompt, so this is used exclusively in
        _open_bedrock_stream() for the initial system prompt. All mid-session
        guidance goes through _send_user_text instead."""
        content_name = str(uuid.uuid4())
        await self._send_bedrock_event({"event": {"contentStart": {
            "promptName": self.prompt_name, "contentName": content_name,
            "type": "TEXT", "role": "SYSTEM", "interactive": False,
            "textInputConfiguration": {"mediaType": "text/plain"},
        }}})
        await self._send_bedrock_event({"event": {"textInput": {
            "promptName": self.prompt_name, "contentName": content_name, "content": text,
        }}})
        await self._send_bedrock_event({"event": {"contentEnd": {
            "promptName": self.prompt_name, "contentName": content_name,
        }}})

    async def _send_user_text(self, text: str):
        """Sends a synthetic USER-role text turn to prompt a model reply.
        Used for the opening greeting nudge, time-status notes, silence
        reprompts, and stall-recovery nudges — wherever a text cue is needed
        without real candidate audio."""
        await self._send_text("USER", text)

    async def _send_text(self, role: str, text: str):
        """Sends one non-interactive TEXT content block with the given role
        (USER or ASSISTANT); also used to replay chat history."""
        content_name = str(uuid.uuid4())
        await self._send_bedrock_event({"event": {"contentStart": {
            "promptName": self.prompt_name, "contentName": content_name,
            "type": "TEXT", "role": role, "interactive": False,
            "textInputConfiguration": {"mediaType": "text/plain"},
        }}})
        await self._send_bedrock_event({"event": {"textInput": {
            "promptName": self.prompt_name, "contentName": content_name, "content": text,
        }}})
        await self._send_bedrock_event({"event": {"contentEnd": {
            "promptName": self.prompt_name, "contentName": content_name,
        }}})

    async def _send_tool_result(self, tool_use_id: str, output: dict):
        content_name = str(uuid.uuid4())
        await self._send_bedrock_event({"event": {"contentStart": {
            "promptName": self.prompt_name, "contentName": content_name,
            "interactive": False, "type": "TOOL", "role": "TOOL",
            "toolResultInputConfiguration": {
                "toolUseId": tool_use_id, "type": "TEXT",
                "textInputConfiguration": {"mediaType": "text/plain"},
            },
        }}})
        await self._send_bedrock_event({"event": {"toolResult": {
            "promptName": self.prompt_name, "contentName": content_name, "content": json.dumps(output),
        }}})
        await self._send_bedrock_event({"event": {"contentEnd": {
            "promptName": self.prompt_name, "contentName": content_name,
        }}})

    # ── Bedrock stream lifecycle ──────────────────────────────────────────────

    def _current_system_prompt(self) -> str:
        """Selects the appropriate system prompt text for the current phase."""
        if not self.module2_mode:
            return build_interview_prompt(self.details)
        if self.audio_check_passed:
            return build_interview_prompt(self.details)
        if self.audio_check_attempts > 0:
            return build_audio_retry_prompt(self.details)
        return build_audio_check_prompt(self.details)

    async def _open_bedrock_stream(self, carry_forward_note: str | None = None):
        """(Re)opens a Bedrock bidirectional stream: sessionStart, promptStart
        (with tools/voice config), the system prompt (plus an optional
        carry-forward progress note after a session-rotation reconnect), and
        one long-lived AUDIO/USER content block that stays open for the rest
        of this session — mirroring AWS's sample, where the entire call's
        microphone audio streams into a single contentStart/.../contentEnd
        cycle rather than one cycle per utterance."""
        logger.info("Opening Bedrock bidirectional stream for model %s.", settings.bedrock_model_id)
        stream_response = await self.bedrock_client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(model_id=settings.bedrock_model_id)
        )
        self.stream_response = stream_response
        self.connection_count += 1
        self.prompt_name = str(uuid.uuid4())
        self.audio_content_name = str(uuid.uuid4())

        await self._send_bedrock_event({"event": {"sessionStart": {
            "inferenceConfiguration": {"maxTokens": 1024, "topP": 0.9, "temperature": 0.7},
            "turnDetectionConfiguration": {"endpointingSensitivity": "LOW"},
        }}})
        await self._send_bedrock_event({"event": {"promptStart": {
            "promptName": self.prompt_name,
            "textOutputConfiguration": {"mediaType": "text/plain"},
            "audioOutputConfiguration": {
                "mediaType": "audio/lpcm", "sampleRateHertz": AUDIO_OUTPUT_SAMPLE_RATE, "sampleSizeBits": 16,
                "channelCount": 1, "voiceId": settings.bedrock_voice_id, "encoding": "base64", "audioType": "SPEECH",
            },
            "toolUseOutputConfiguration": {"mediaType": "application/json"},
            "toolConfiguration": {"tools": self.tools},
        }}})

        system_text = self._current_system_prompt()
        if carry_forward_note:
            system_text = system_text + carry_forward_note
        await self._send_system_prompt(system_text)

        self.stream_has_history = False
        if carry_forward_note and self.replay_history:
            self.stream_has_history = True
            # Replay the conversation as real chat history (non-interactive
            # text turns) so the new session knows what was actually said,
            # not just a summary inside the system prompt.
            history = self._build_history_messages()
            for role, text in history:
                await self._send_text(role, text)
            logger.info("Replayed %d chat-history messages into the new Bedrock session.", len(history))

        await self._send_bedrock_event({"event": {"contentStart": {
            "promptName": self.prompt_name, "contentName": self.audio_content_name,
            "type": "AUDIO", "interactive": True, "role": "USER",
            "audioInputConfiguration": {
                "mediaType": "audio/lpcm", "sampleRateHertz": AUDIO_INPUT_SAMPLE_RATE, "sampleSizeBits": 16,
                "channelCount": 1, "audioType": "SPEECH", "encoding": "base64",
            },
        }}})

    async def _close_bedrock_stream(self):
        if not self.stream_response:
            return
        try:
            await self._send_bedrock_event({"event": {"contentEnd": {
                "promptName": self.prompt_name, "contentName": self.audio_content_name,
            }}})
            await self._send_bedrock_event({"event": {"promptEnd": {"promptName": self.prompt_name}}})
            await self._send_bedrock_event({"event": {"sessionEnd": {}}})
            await self.stream_response.input_stream.close()
        except Exception as e:
            logger.warning(f"Error closing Bedrock stream cleanly: {e}")

    # ── Session carry-forward ─────────────────────────────────────────────────

    def _build_history_messages(self) -> list[tuple[str, str]]:
        """Turns the transcript into (role, text) chat-history messages for
        replay into a fresh Bedrock session. Consecutive sentences from the
        same speaker are merged into one message, each message is capped,
        and the oldest messages are dropped to stay within the total budget.
        History must begin with a USER message, so leading AI turns are dropped."""
        messages: list[list[str]] = []
        for turn in self.session_logger.turns:
            role = "USER" if turn["speaker"] == "CANDIDATE" else "ASSISTANT"
            text = turn["text"].strip()
            if not text:
                continue
            if messages and messages[-1][0] == role:
                messages[-1][1] += " " + text
            else:
                messages.append([role, text])

        history: list[tuple[str, str]] = []
        total = 0
        for role, text in reversed(messages):
            text = text[-HISTORY_MESSAGE_MAX_CHARS:]
            if total + len(text) > HISTORY_TOTAL_MAX_CHARS:
                break
            history.append((role, text))
            total += len(text)
        history.reverse()
        while history and history[0][0] != "USER":
            history.pop(0)
        return history

    def _build_carry_forward_note(self) -> str:
        if self.closing_spoken:
            question_context = (
                "The interview is FINISHED and you have already given your closing remarks. "
                "Do NOT ask any questions or resume the interview. If the candidate says "
                "anything, reply with one short goodbye and then invoke end_call."
            )
        elif self.current_question_id and self.candidate_speaking:
            question_context = (
                f"The candidate was answering question {self.current_question_id} when the "
                "session reconnected and their audio was cut off. Apologise briefly for the "
                "interruption and ask them to continue their answer — do not re-read the "
                "question word for word."
            )
        elif self.current_question_id:
            question_context = (
                f"Question {self.current_question_id} is in progress. Continue from the "
                "conversation history: respond to the candidate's latest answer, or keep "
                "waiting for it if they have not answered yet."
            )
        elif self.completed_question_ids:
            snapshot = (self.details.get("assessment_plan_snapshot") or {})
            total_questions = len(snapshot.get("questions") or [])
            all_done = total_questions > 0 and len(self.completed_question_ids) >= total_questions
            if all_done:
                question_context = (
                    f"All {total_questions} required questions have been completed "
                    f"({', '.join(self.completed_question_ids)}). "
                    "Do NOT ask any more questions. Proceed directly to your closing remarks "
                    "and then invoke end_call."
                )
            else:
                question_context = (
                    f"Questions completed so far: {', '.join(self.completed_question_ids)}. "
                    "Continue with the next unanswered question from the required list."
                )
        else:
            question_context = (
                "No interview question has been completed. Continue from the latest conversation "
                "turn; do not repeat the greeting or any question already spoken."
            )

        return (
            "\n\nIMPORTANT SESSION CONTINUATION: This is a technical reconnect during the same "
            "interview. Do not introduce yourself again, restart the interview, repeat a greeting, "
            "or repeat a question. The conversation so far is provided as chat history; treat it "
            "and the progress below as authoritative. Continue naturally with the next unanswered "
            "part; if the last assistant sentence was cut off, briefly finish that thought without "
            "repeating it.\n"
            f"{question_context}"
        )

    # ── Background watchers ───────────────────────────────────────────────────

    async def _watch_camera(self):
        """Nudges the candidate to turn on their camera if it stays off.
        Waits for the interview to start (so it never interrupts the audio
        check), then nudges after 8 seconds and every 120 seconds."""
        await self.interview_started_event.wait()
        await asyncio.sleep(8)
        while not self.end_call_event.is_set():
            if not self.candidate_video_on:
                for _ in range(10):
                    if not self.ai_responding:
                        break
                    await asyncio.sleep(1)
                logger.info("Candidate camera is off; nudging to turn it on.")
                await self._send_user_text(
                    "(SYSTEM NOTE: The candidate's camera is still off. Politely remind "
                    "them to turn on their video so you can see them during the interview. "
                    "Then continue the interview from where you left off.)"
                )
            await asyncio.sleep(120)
            if self.candidate_video_on:
                await asyncio.sleep(120)

    async def _watch_silence(self):
        while not self.end_call_event.is_set():
            await asyncio.sleep(2)
            started = self.waiting_since
            if started is None or self.ai_responding or self.silence_nudge_sent:
                continue
            if time.monotonic() - started >= SILENCE_REPROMPT_SECONDS:
                self.silence_nudge_sent = True
                logger.info("Candidate has been silent for a while; nudging for a check-in.")
                await self._send_user_text(
                    "(SYSTEM NOTE: The candidate has been silent for a while. "
                    "Do NOT move to the next question. Simply ask if they would like "
                    "the question repeated or need more time to think. Do not rephrase "
                    "the question or give any hints. Wait for their response before proceeding.)"
                )

    async def _watch_question_budget(self):
        """Tells the model once per question when its target_seconds budget
        is used up, so it skips the remaining follow-ups."""
        while not self.end_call_event.is_set():
            await asyncio.sleep(5)
            started, target = self.question_started_at, self.question_target_seconds
            if started is None or not target or self.question_budget_warned or self.ai_responding:
                continue
            if time.monotonic() - started >= target:
                self.question_budget_warned = True
                logger.info("Time budget for question %s used up; telling the model to skip further follow-ups.", self.current_question_id)
                await self._send_user_text(
                    "(SYSTEM NOTE: The time budget for this question is used up. Ask no more "
                    "follow-ups for it. After the candidate finishes their current answer, "
                    "acknowledge briefly, call question_completed, and move to the next question.)"
                )

    async def _watch_rotation(self):
        """Nova Sonic connections are capped at ~8 minutes. Proactively
        reconnect before that limit with a short carry-forward progress note."""
        while not self.end_call_event.is_set():
            await asyncio.sleep(15)
            if time.monotonic() - self.session_started_at < SESSION_ROTATE_AFTER_SECONDS:
                continue
            if self.end_call_event.is_set():
                return
            if self.closing_spoken:
                continue  # the call is about to end; a fresh session would only risk resuming the interview
            async with self.bedrock_stream_lock:
                elapsed = time.monotonic() - self.session_started_at
                if elapsed < SESSION_ROTATE_AFTER_SECONDS:
                    continue
                safe_to_rotate = (
                    not self.ai_responding
                    and not self.candidate_speaking
                    and self.audio_playback_end_time <= time.monotonic()
                )
                if not safe_to_rotate and elapsed < SESSION_ROTATE_FORCE_AFTER_SECONDS:
                    continue
                if not safe_to_rotate:
                    logger.warning("Bedrock stream rotation reached its safety deadline during active audio.")
                logger.info("Rotating to a fresh Bedrock session before the ~8-minute connection cap.")
                if self.candidate_speaking:
                    self._mark_question_technical_issue("answer_cut_off_by_reconnect")
                self._flush_ai_turn()
                old_stream = self.stream_response
                try:
                    try:
                        await self.websocket.send(json.dumps({"type": "stop_audio"}))
                    except Exception:
                        logger.debug("Could not send playback stop before Bedrock rotation.", exc_info=True)
                    if old_stream:
                        try:
                            await old_stream.input_stream.close()
                        except Exception:
                            logger.debug("Old Bedrock stream input was already closed during rotation.", exc_info=True)
                        if old_stream.output_stream:
                            try:
                                await old_stream.output_stream.close()
                            except Exception:
                                logger.debug("Old Bedrock stream output was already closed during rotation.", exc_info=True)
                    await self._open_bedrock_stream(carry_forward_note=self._build_carry_forward_note())
                    self.session_started_at = time.monotonic()
                    logger.info("Session rotation completed.")
                except Exception:
                    logger.exception("Session rotation failed (continuing on the existing connection).")

    async def _run_timer(self):
        await self.interview_started_event.wait()

        total_seconds = self.details.get("duration_minutes", 30) * 60
        start = time.monotonic()
        while not self.end_call_event.is_set():
            await asyncio.sleep(TIME_UPDATE_INTERVAL_SECONDS)
            elapsed = time.monotonic() - start
            remaining = total_seconds - elapsed

            for _ in range(10):
                if not self.ai_responding:
                    break
                await asyncio.sleep(1)

            await self._send_user_text("(SYSTEM NOTE: " + render_time_status(remaining) + ")")

            if remaining <= 0:
                if self.candidate_speaking:
                    waited_for_candidate = 0.0
                    while self.candidate_speaking and waited_for_candidate < TIME_UP_CANDIDATE_WAIT_CAP_SECONDS:
                        await asyncio.sleep(1)
                        waited_for_candidate += 1
                    if self.candidate_speaking:
                        logger.warning("Candidate still speaking past the time-up wait cap; forcing closing response.")
                        await self._send_user_text("(Time has ended. Give your closing statement now and then end the call.)")
                else:
                    await self._send_user_text("(Time has ended. Give your closing statement now and then end the call.)")
                try:
                    await asyncio.wait_for(self.end_call_event.wait(), timeout=HARD_CUTOFF_GRACE_SECONDS)
                except asyncio.TimeoutError:
                    logger.warning("Model didn't call end_call within grace period, forcing hangup.")
                    self.end_call_event.set()
                return

    async def _handle_hangup(self):
        await self.end_call_event.wait()
        waited = 0.0
        while self.ai_responding and waited < 20.0:
            await asyncio.sleep(0.2)
            waited += 0.2

        now = time.monotonic()
        remaining_playback = self.audio_playback_end_time - now
        if remaining_playback > 0:
            play_wait = min(remaining_playback, 30.0)
            logger.info(f"Waiting {play_wait:.2f}s for AI closing speech playback to complete.")
            await asyncio.sleep(play_wait)

        await asyncio.sleep(1.5)

        # Only a candidate still talking proves the answer was cut off; a
        # question the model forgot to mark completed may be fully answered.
        if self.candidate_speaking:
            self._mark_question_technical_issue("call_ended_mid_question")
        self._flush_ai_turn()

        if self.module2_mode:
            await self._transition("CLOSING", "call ending")
            self._log_event("INTERVIEW_COMPLETED")
        if self.end_call_callback:
            try:
                await self.end_call_callback()
            except Exception as e:
                logger.error(f"Teams hangup callback failed: {e}")
        await self._close_bedrock_stream()
        await self.websocket.close()

    # ── Tool handler ──────────────────────────────────────────────────────────

    async def _handle_tool_call(self, tool_name: str, tool_use_id: str, arguments: dict):
        if tool_name == "end_call":
            logger.info(f"Model requested end_call: {arguments.get('reason', 'no reason given')}")
            await self._send_tool_result(tool_use_id, {"status": "ending_call"})
            self.end_call_event.set()

        elif tool_name == "report_audio_check" and self.module2_mode:
            confirmed = bool(arguments.get("candidate_confirmed"))
            self.audio_check_attempts += 1

            if self.pg_session_id and self.ai_interview_repo:
                _fire_and_forget(
                    self.ai_interview_repo.record_audio_check_attempt(self.pg_session_id, confirmed),
                    "audio_check_attempt",
                )

            if confirmed:
                self.audio_check_passed = True
                logger.info("Audio check passed.")
                self._log_event("AUDIO_CHECK_PASSED")
                await self._transition("INTERVIEWING", "audio check confirmed by candidate")
                if self.pg_session_id and self.ai_interview_repo:
                    _fire_and_forget(
                        self.ai_interview_repo.mark_session_started(self.pg_session_id),
                        "session_started",
                    )
                self.interview_started_event.set()

                # CRITICAL: In Nova Sonic, returning the tool result causes the model
                # to respond immediately to that tool result.
                # If we send a separate _send_user_text, it creates a second competing turn / double response.
                # Instead, include the interview instructions directly in the tool result instruction context.
                snapshot = self.details.get("assessment_plan_snapshot") or {}
                plan_questions = sorted(snapshot.get("questions", []), key=lambda q: q.get("priority", 999))
                first_qid = plan_questions[0].get("question_id") if plan_questions else "q1"

                await self._send_tool_result(tool_use_id, {
                    "status": "audio_check_passed",
                    "instruction": (
                        "The candidate confirmed they hear you clearly. "
                        "The audio check is finished. NOW begin the interview immediately: "
                        f"Briefly introduce yourself, ask {self.details.get('candidate', 'the candidate')} to turn on their camera if it is off, "
                        f"and directly ask the first question ({first_qid}) exactly as written, with no hints. "
                        "Do NOT say 'Great, thank you, we can proceed' and stop. "
                        "Speak your introduction, camera request, and the first question together in ONE continuous response right now."
                    ),
                    "interview_rules_and_questions": build_interview_prompt(self.details)
                })

            elif self.audio_check_attempts < MAX_AUDIO_CHECK_ATTEMPTS:
                logger.warning(f"Audio check failed (attempt {self.audio_check_attempts}); retrying.")
                self._log_event("AUDIO_CHECK_FAILED", {"attempt": self.audio_check_attempts, "final": False})
                await self._send_tool_result(tool_use_id, {
                    "status": "audio_check_failed_retry",
                    "instruction": build_audio_retry_prompt(self.details)
                })
            else:
                logger.error("Audio check failed twice; marking TECHNICAL_FAILURE.")
                self._log_event("AUDIO_CHECK_FAILED", {"attempt": self.audio_check_attempts, "final": True})
                self._log_event("TECHNICAL_FAILURE", {"reason": "audio check failed twice"})
                await self._transition("TECHNICAL_FAILURE", "audio check failed twice")
                if self.pg_session_id and self.ai_interview_repo:
                    _fire_and_forget(
                        self.ai_interview_repo.set_technical_failure(self.pg_session_id, "audio check failed twice", True),
                        "technical_failure",
                    )
                await self._send_tool_result(tool_use_id, {
                    "status": "audio_check_failed_terminal",
                    "instruction": build_audio_failure_prompt(self.details)
                })
                asyncio.create_task(self._timeout_audio_check())

        elif tool_name == "question_started" and self.module2_mode:
            qid = arguments.get("question_id")
            self.current_question_id = qid
            snapshot = self.details.get("assessment_plan_snapshot") or {}
            question = next((q for q in snapshot.get("questions", []) if str(q.get("question_id")) == str(qid)), {})
            self.question_started_at = time.monotonic()
            self.question_target_seconds = question.get("target_seconds")
            self.question_budget_warned = False
            logger.info(f"Question started: {qid}")
            self._log_event("QUESTION_STARTED", {"question_id": qid})
            await self._send_tool_result(tool_use_id, {"status": "recorded"})

        elif tool_name == "question_completed" and self.module2_mode:
            qid = arguments.get("question_id")
            follow_up_count = arguments.get("follow_up_count")
            follow_up_used = bool(arguments.get("follow_up_used")) or bool(follow_up_count)
            logger.info(f"Question completed: {qid} (follow_up_used={follow_up_used}, follow_up_count={follow_up_count})")
            self._log_event("QUESTION_COMPLETED", {
                "question_id": qid, "follow_up_used": follow_up_used, "follow_up_count": follow_up_count,
            })
            if qid:
                self.completed_question_ids.append(qid)
            if self.pg_session_id and self.ai_interview_repo:
                _fire_and_forget(
                    self.ai_interview_repo.increment_questions_completed(self.pg_session_id),
                    "questions_completed",
                )
            self.current_question_id = None
            self.question_started_at = None
            tool_res_payload = {"status": "recorded"}
            if not self.candidate_video_on:
                tool_res_payload["reminder"] = (
                    "The candidate's video/camera appears to be turned off. "
                    "Politely ask them to turn on their camera, then continue with the next question in the same turn."
                )
            await self._send_tool_result(tool_use_id, tool_res_payload)
            asyncio.create_task(self._nudge_if_stalled())

        else:
            await self._send_tool_result(tool_use_id, {"status": "ignored"})

    async def _timeout_audio_check(self):
        """Forces hangup if the model doesn't call end_call after a terminal audio-check failure."""
        try:
            await asyncio.wait_for(self.end_call_event.wait(), timeout=AUDIO_CHECK_FAILURE_GRACE_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("Model didn't call end_call after audio-check failure; forcing hangup.")
            self.end_call_event.set()

    async def _nudge_if_stalled(self):
        """After question_completed, nudges the model if it ends its turn
        without asking the next question."""
        for attempt in range(2):
            await asyncio.sleep(15)
            waited = 0.0
            while self.ai_responding and waited < 25.0:
                await asyncio.sleep(0.3)
                waited += 0.3
            if self.end_call_event.is_set() or self.current_question_id is not None:
                return
            if not self.ai_responding:
                logger.warning(
                    "Model ended its turn after question_completed without asking the next question (attempt %d).",
                    attempt + 1,
                )
                await self._send_user_text(
                    "(You must ask the next interview question NOW in this response. "
                    "Do not just say you will continue — actually ask the question. "
                    "If all questions are complete, give your closing remarks.)"
                )

    # ── Bedrock response reader ───────────────────────────────────────────────

    async def _read_bedrock_responses(self):
        audio_output_chunks = 0
        generation_stage = "FINAL"
        while True:
            stream_response = self.stream_response
            if not stream_response:
                await asyncio.sleep(0.1)
                continue
            try:
                output = await stream_response.await_output()
                result = await output[1].receive()
                if result is None:
                    self.ai_responding = False
                    async with self.bedrock_stream_lock:
                        if self.stream_response is stream_response:
                            logger.warning("Bedrock output stream ended; opening a replacement stream.")
                            if self.candidate_speaking:
                                self._mark_question_technical_issue("answer_cut_off_by_reconnect")
                            self._flush_ai_turn()
                            self.stream_response = None
                            try:
                                await stream_response.input_stream.close()
                            except Exception:
                                logger.debug("Ended Bedrock stream input was already closed.", exc_info=True)
                            while not self.end_call_event.is_set() and self.stream_response is None:
                                try:
                                    await self._open_bedrock_stream(
                                        carry_forward_note=self._build_carry_forward_note()
                                    )
                                except Exception:
                                    logger.exception("Could not reopen ended Bedrock stream; retrying in 5 seconds.")
                                    await asyncio.sleep(5)
                                else:
                                    self.session_started_at = time.monotonic()
                                    logger.info("Replacement Bedrock stream opened.")
                        else:
                            logger.info("Retired Bedrock stream ended after rotation; continuing on the new stream.")
                    continue

                if not (result.value and result.value.bytes_):
                    continue
                response_data = result.value.bytes_.decode("utf-8")
                json_data = json.loads(response_data)
                event = json_data.get("event", {})

                if "textOutput" in event:
                    text_out = event["textOutput"]
                    text_content = text_out.get("content", "")
                    role = str(text_out.get("role") or "").upper()
                    # Barge-in signal: confirmed against AWS's sample code --
                    # not a separate event type, a sentinel string inside a
                    # textOutput event. Nova Sonic auto-interrupts itself when
                    # it detects speech in the continuously-open audio input
                    # stream; this just tells us it already happened so we
                    # can mute whatever's still queued for playback locally.
                    if '"interrupted"' in text_content:
                        self.ai_responding = False
                        try:
                            await self.websocket.send(json.dumps({"type": "stop_audio"}))
                        except Exception:
                            pass
                        continue
                    if text_content.strip():
                        if role == "USER":
                            # FINAL AI text can land after the audio turn end;
                            # flush here too so it is stored before this answer.
                            self._flush_ai_turn()
                            self.session_logger.log_turn("CANDIDATE", text_content)
                            if self.module2_mode and self.pg_session_id and self.ai_interview_repo:
                                _fire_and_forget(
                                    self.ai_interview_repo.insert_transcript_turn(
                                        self.pg_session_id, "CANDIDATE", text_content,
                                        question_id=self.current_question_id,
                                    ),
                                    "transcript:candidate",
                                )
                        elif role == "ASSISTANT" and generation_stage == "SPECULATIVE":
                            pass
                        elif role == "ASSISTANT":
                            if "can leave the meeting" in text_content.lower():
                                self.closing_spoken = True
                            self.session_logger.log_turn("AI_INTERVIEWER", text_content)
                            self.ai_turn_buffer.append(text_content.strip())
                        else:
                            logger.info("Bedrock transcript (role=%r): %s", role, text_content)

                elif "audioOutput" in event:
                    self.ai_responding = True
                    if self.latency_started_at is not None:
                        latency_ms = int((time.monotonic() - self.latency_started_at) * 1000)
                        self.latency_started_at = None
                        if latency_ms <= 60_000:  # longer gaps are silence nudges, not replies
                            self.response_latencies_ms.append(latency_ms)
                    audio_content = event["audioOutput"].get("content")
                    if audio_content:
                        pcm_bytes = base64.b64decode(audio_content)
                        if audio_output_chunks == 0:
                            logger.info(
                                "Bedrock emitted its first audioOutput chunk (%d PCM bytes).",
                                len(pcm_bytes),
                            )
                            # Close the capture gate on the first audio chunk so
                            # Teams' loopback of the AI's voice doesn't echo back
                            # into Nova Sonic as fake candidate speech.
                            try:
                                await self.websocket.send(json.dumps({"type": "mute_capture", "mute": True}))
                            except Exception:
                                pass
                        audio_output_chunks += 1
                        chunk_duration = len(pcm_bytes) / (AUDIO_OUTPUT_SAMPLE_RATE * 2)
                        now = time.monotonic()
                        self.audio_playback_end_time = max(now, self.audio_playback_end_time) + chunk_duration
                        try:
                            await self.websocket.send(pcm_bytes)
                        except Exception as ws_err:
                            # Browser WebSocket dropped (Playwright crash, Teams
                            # disconnect, ping timeout). Stop processing — there
                            # is nowhere to send audio and the outer message loop
                            # will also see the closure and exit cleanly.
                            logger.warning(f"Browser WebSocket closed while sending audio ({type(ws_err).__name__}); stopping response loop.")
                            self.end_call_event.set()
                            return

                elif "toolUse" in event:
                    tool_use = event["toolUse"]
                    tool_name = tool_use.get("toolName")
                    tool_use_id = tool_use.get("toolUseId")
                    raw_content = tool_use.get("content", "{}")
                    try:
                        arguments = json.loads(raw_content) if isinstance(raw_content, str) else (raw_content or {})
                    except json.JSONDecodeError:
                        arguments = {}
                    await self._handle_tool_call(tool_name, tool_use_id, arguments)

                elif "contentStart" in event:
                    # Nova Sonic sends each assistant sentence twice: a
                    # SPECULATIVE preview and a FINAL transcript. Remember the
                    # stage so only FINAL text is logged/persisted.
                    fields = event["contentStart"].get("additionalModelFields")
                    try:
                        fields = json.loads(fields) if isinstance(fields, str) else (fields or {})
                    except json.JSONDecodeError:
                        fields = {}
                    generation_stage = str(fields.get("generationStage") or "FINAL").upper()

                elif "contentEnd" in event or "completionEnd" in event:
                    # Nova Sonic only emits completionEnd when the whole prompt
                    # ends, so the real per-turn end is an AUDIO contentEnd
                    # with stopReason END_TURN (or INTERRUPTED on barge-in).
                    # Without this the capture gate stayed closed after the
                    # first AI utterance and the candidate was never heard.
                    if "contentEnd" in event:
                        content_end = event["contentEnd"]
                        if content_end.get("type") != "AUDIO" or content_end.get("stopReason") not in ("END_TURN", "INTERRUPTED"):
                            continue
                    self.ai_responding = False
                    self._flush_ai_turn()
                    if self.closing_spoken and not self.end_call_event.is_set():
                        # The model said goodbye but forgot end_call; hang up
                        # ourselves instead of leaving the call open.
                        logger.warning("Closing remarks spoken without end_call; ending the call.")
                        self.end_call_event.set()
                    self.waiting_since = time.monotonic()
                    self.silence_nudge_sent = False
                    audio_output_chunks = 0  # reset for next turn
                    # Reopen the capture gate after a short delay so the audio
                    # that's still buffered for playback (scheduled ahead) has
                    # time to finish playing before we start capturing again.
                    playback_remaining = self.audio_playback_end_time - time.monotonic()
                    # Extra 2.5 s buffer covers Teams' conference-loopback
                    # latency (~1-2 s round-trip through Teams cloud). Without
                    # it the gate reopens while the AI's own echo is still
                    # arriving, causing Bedrock to respond to its own voice.
                    reopen_delay = max(0.2, playback_remaining + 2.5)
                    async def _reopen_capture(delay: float):
                        await asyncio.sleep(delay)
                        try:
                            await self.websocket.send(json.dumps({"type": "mute_capture", "mute": False}))
                        except Exception:
                            pass
                    asyncio.create_task(_reopen_capture(reopen_delay))

                elif "usageEvent" in event:
                    usage = event["usageEvent"]
                    if not self.usage_event_logged:
                        logger.info("First Bedrock usageEvent (raw): %s", json.dumps(usage))
                        self.usage_event_logged = True
                    delta = (usage.get("details") or {}).get("delta") or {}
                    delta_in = delta.get("input") or {}
                    delta_out = delta.get("output") or {}
                    self.usage_totals["input_speech"] += int(delta_in.get("speechTokens") or 0)
                    self.usage_totals["input_text"] += int(delta_in.get("textTokens") or 0)
                    self.usage_totals["output_speech"] += int(delta_out.get("speechTokens") or 0)
                    self.usage_totals["output_text"] += int(delta_out.get("textTokens") or 0)

                elif "completionStart" in event:
                    pass  # no action needed

            except asyncio.CancelledError:
                raise
            except Exception as e:
                # If the browser WebSocket closed, stop immediately — there is
                # nothing left to send audio to and retrying just spams logs.
                from websockets.exceptions import ConnectionClosedError as _WsClosedError
                if isinstance(e, _WsClosedError):
                    logger.warning(f"Browser WebSocket closed ({e}); stopping response loop.")
                    self.end_call_event.set()
                    return
                if self.stream_response is not stream_response:
                    # Expected: the stream we were reading was closed by rotation.
                    logger.info("Retired Bedrock stream closed after rotation; continuing on the new stream.")
                    continue
                # Nova Sonic surfaces errors as stream exceptions (e.g.
                # ValidationException), not a distinguishable {"type":"error"}
                # event like Azure's Realtime API -- confirmed from AWS's own
                # sample code, which catches broadly here too.
                logger.error(f"Bedrock stream error ({type(e).__name__}): {e}", exc_info=True)
                if type(e).__name__ == "ValidationException" and self.stream_has_history:
                    # Without this, every replacement stream replays the same
                    # rejected history and fails again in a tight loop.
                    logger.warning("Bedrock rejected the replayed chat history; reconnecting without it for the rest of this call.")
                    self.replay_history = False
                self.ai_responding = False
                await asyncio.sleep(1)  # avoid a tight error loop if the stream is persistently broken

    # ── Main run loop ─────────────────────────────────────────────────────────

    async def run(self):
        logger.info("Browser audio engine connected to routing matrix.")

        if self.module2_mode and self.redis_interview_id:
            # Local imports: keeps this module importable without Postgres/Redis
            # configured for legacy callers that don't use module2_mode.
            from app.state import machine as state_machine
            from app.repositories import ai_interview_repo
            self.state_machine = state_machine
            self.ai_interview_repo = ai_interview_repo

        self.bedrock_client = await _build_bedrock_client()

        response_task = timer_task = hangup_task = silence_task = rotation_task = camera_task = budget_task = None

        try:
            # Open the Bedrock session as soon as the bot's WebSocket connects
            # so it is fully warmed up before the candidate joins. The greeting
            # nudge is still held until participant_joined fires, so the model
            # stays silent until the candidate actually arrives.
            await self._open_bedrock_stream()
            response_task = asyncio.create_task(self._read_bedrock_responses())

            try:
                async for message in self.websocket:
                    if isinstance(message, str):
                        data = json.loads(message)
                        if data.get("type") == "participant_joined":
                            if not self.greeted:
                                self.greeted = True
                                if self.module2_mode:
                                    self._log_event("CANDIDATE_JOINED")
                                    await self._transition("AUDIO_CHECK", "candidate speech detected; starting audio check")
                                timer_task = asyncio.create_task(self._run_timer())
                                hangup_task = asyncio.create_task(self._handle_hangup())
                                silence_task = asyncio.create_task(self._watch_silence())
                                rotation_task = asyncio.create_task(self._watch_rotation())
                                camera_task = asyncio.create_task(self._watch_camera())
                                budget_task = asyncio.create_task(self._watch_question_budget())
                                await self._send_user_text("(The candidate has just joined. Greet them now.)")

                        elif data.get("type") == "speaking_status":
                            is_speaking = bool(data.get("speaking", False))
                            self.candidate_speaking = is_speaking
                            if is_speaking:
                                self.latency_started_at = None
                                self.waiting_since = None
                                self.silence_nudge_sent = False
                                # No explicit response.cancel/input_audio_buffer.clear
                                # equivalent exists in this protocol -- Nova Sonic
                                # auto-interrupts itself from the continuously-open
                                # audio stream and tells us via the textOutput
                                # "interrupted" signal (handled in
                                # _read_bedrock_responses), so there's nothing to
                                # proactively send here.
                            elif self.interview_started_event.is_set():
                                # Candidate finished a turn: time until the AI's
                                # first audioOutput chunk is the reply latency.
                                self.latency_started_at = time.monotonic()

                        elif data.get("type") == "video_status":
                            self.candidate_video_on = bool(data.get("enabled"))

                    elif isinstance(message, bytes):
                        await self._send_bedrock_event({"event": {"audioInput": {
                            "promptName": self.prompt_name,
                            "contentName": self.audio_content_name,
                            "content": base64.b64encode(message).decode("utf-8"),
                        }}})

            finally:
                tasks = [t for t in (response_task, timer_task, hangup_task, silence_task, rotation_task, camera_task, budget_task) if t is not None]
                for t in tasks:
                    t.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                self._flush_ai_turn()
                if self.module2_mode and self.pg_session_id and self.ai_interview_repo:
                    _fire_and_forget(
                        self.ai_interview_repo.save_full_transcript(
                            self.pg_session_id, self.session_logger.build_payload()
                        ),
                        "full_transcript",
                    )
                    self._record_llm_usage()
                    try:
                        await self.ai_interview_repo.mark_session_ended(self.pg_session_id)
                    except Exception as e:
                        logger.warning(f"Could not mark session ended in Postgres: {e}")

        except Exception as e:
            from websockets.exceptions import ConnectionClosedError as _WsClosedError
            if isinstance(e, _WsClosedError):
                logger.warning(f"Browser WebSocket disconnected: {e}")
            else:
                logger.exception(f"Realtime WebSocket Processing Exception: {e}")
        finally:
            if self.bedrock_client:
                try:
                    await self.bedrock_client.close()
                except Exception as e:
                    logger.warning(f"Failed to close Bedrock client: {e}")


# ── Public entry point ────────────────────────────────────────────────────────

async def handle_voice_bridge_ws(websocket, details: dict, end_call_callback=None):
    session = VoiceBridgeSession(websocket, details, end_call_callback)
    await session.run()
