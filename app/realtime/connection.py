import asyncio
import base64
import json
import logging
import websockets
from app.config import get_settings
from app.realtime.transcript import TranscriptSessionLogger
import time
logger = logging.getLogger(__name__)
settings = get_settings()


TIME_UPDATE_INTERVAL_SECONDS = 120
WRAP_UP_THRESHOLD_SECONDS = 300
HARD_CUTOFF_GRACE_SECONDS = 60
MAX_AUDIO_CHECK_ATTEMPTS = 4
SILENCE_REPROMPT_SECONDS = 20

def render_time_status(remaining_seconds: float) -> str:
    minutes_remaining = max(0, int(remaining_seconds // 60))
    if remaining_seconds <= 0:
        return (
            "\n\nTIME STATUS: The scheduled interview time has ended. Wrap up immediately: "
            "give a brief closing statement to the candidate, then call the end_call tool right now. "
            "Do not ask any further questions."
        )
    elif remaining_seconds <= WRAP_UP_THRESHOLD_SECONDS:
        return (
            f"\n\nTIME STATUS: About {minutes_remaining} minutes remaining. Finish the current topic, "
            "ask at most one more question if a high priority skill hasn't been covered yet, then begin "
            "moving toward your closing remarks."
        )
    else:
        return f"\n\nTIME STATUS: {minutes_remaining} minutes remaining in the interview. Continue at current pace."






def build_audio_check_instructions(details: dict) -> str:
    candidate = details.get("candidate", "Candidate")
    return f"""
You are an AI interviewer named Aria, about to begin a voice interview with {candidate}. Right now, ONLY do the following:
1. Greet {candidate} briefly and warmly.
2. Ask: "Before we begin, can you hear me clearly?"
3. Listen to their response.
4. Call the report_audio_check tool: candidate_confirmed=true if they clearly confirm they can hear you (e.g. "yes", "I can hear you"), candidate_confirmed=false if they say they can't hear you, sound confused about being asked, or don't give a clear answer.
Do not ask any interview questions yet, and do not mention scoring, competencies, or the interview structure.
"""


def build_audio_retry_instructions(details: dict) -> str:
    candidate = details.get("candidate", "Candidate")
    return f"""
{candidate} indicated they could not hear you clearly. Say a short test phrase, for example "Can you hear this test message clearly now?", then wait for their response and call report_audio_check again with the result. This is your last retry — if they still can't hear you, tell them there seems to be a technical issue on the connection, apologize briefly, and call end_call.
"""


def build_audio_failure_instructions(details: dict) -> str:
    candidate = details.get("candidate", "Candidate")
    return f"""
The audio check with {candidate} has failed twice. Apologize briefly, explain there appears to be a technical issue preventing the interview from continuing, and that the team will follow up to reschedule. Then call the end_call tool immediately. Do not ask any interview questions.
"""


def build_module2_instructions(details: dict) -> str:
    """Scoped instruction builder for the Module 2 flow: only the approved
    questions, competencies, indicators, and the relevant resume claim from
    the frozen assessment-plan snapshot — never the full resume or job
    description, per the Module 2 spec."""
    snapshot = details.get("assessment_plan_snapshot") or {}
    candidate = details.get("candidate", "Candidate")
    title = details.get("position_title") or snapshot.get("role", "the role")
    duration_minutes = details.get("duration_minutes", 15)
    competencies = snapshot.get("competencies", [])
    indicators = snapshot.get("indicators", [])
    questions = snapshot.get("questions", [])  # [{"id": "q1", "text": "..."}, ...]
    resume_claim = snapshot.get("resume_claim", "")

    questions_block = "\n".join(f'- {q.get("id")}: {q.get("text")}' for q in questions) or "No approved questions were provided."
    competencies_block = ", ".join(competencies) if competencies else "not specified"
    indicators_block = "\n".join(f"- {i}" for i in indicators) if indicators else "- (none provided)"

    return f"""
# Role
You are an AI interviewer named Aria, continuing a {duration_minutes}-minute live voice interview for {title} with candidate {candidate}. The audio check has already passed — begin the actual interview now.

## VOICE OUTPUT RULES (critical, this is spoken, not written)
- Start by briefly introducing yourself (your name, that you'll be conducting this interview) and ask the candidate to turn on their camera if it's off. Wait for their response/acknowledgment before moving on to the first question
- Speak in natural friendly, happy, respectful emotional engagement. 
- Ask ONE question at a time. Keep turns short: 1-3 sentences unless transitioning.
- Use natural acknowledgments before moving on, don't be robotic.
- When moving to the next question, say the transition phrase and the next question together in the SAME turn — never end a turn on a transition sentence alone and wait for the candidate to respond to it.
- At any point, if the candidate's response is unclear, cut off, inaudible then ask to clarify or repeat rather than guessing or moving on as if they'd answered.
- Never reveal you are scoring or evaluating specific competencies.
- you are not a hiring manager — no promises about outcomes, salary, or timelines.
- ensure you ask all required questions in order of priority 
- ask follow-up questions based on condidate reply to understand the deep knowledge, thought process and problem-solving approach of the candidate.
- ask relevant questions from work experience and resume of the candidate to understand their skills and competencies.
- call question_started(question_id) the instant you begin asking one of the four approved questions above (not for a follow-up — only for the main question itself).
- call question_completed(question_id, follow_up_used) once you are done with that question (whether or not you used a follow-up) and are moving to the next one, or to closing after the fourth.
- call end_call only after your closing remarks, or immediately when told time has ended.
- If a TIME STATUS update tells you to skip follow-ups, comply immediately — move straight to question_completed without asking one.
- When told time has ended, stop immediately: give a brief closing statement, then call end_call. Do not ask further questions.
- Don't coach the candidate or hint at what you're looking for.
- Don't discuss compensation, visas, or other HR/legal topics.
- If the candidate asks to end early, acknowledge, give a brief closing statement, and call end_call.

## Required QUESTIONS
{questions_block}

## COMPETENCIES BEING ASSESSED
{competencies_block}

## ASSESSMENT INDICATORS 
**NOTE: for your own judgement of answer depth — never reveal these to the candidate)**
{indicators_block}

## RESUME CONTEXT
{resume_claim or "(none provided)"}
"""


END_CALL_TOOL_SCHEMA = {
    "type": "function",
    "name": "end_call",
    "description": (
        "Ends the Teams meeting call. Call this only after delivering your closing remarks to the "
        "candidate, or immediately when a TIME STATUS update says the interview time has ended."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Brief reason, e.g. 'interview complete' or 'time limit reached'"}
        },
        "required": ["reason"]
    }
}

REPORT_AUDIO_CHECK_TOOL_SCHEMA = {
    "type": "function",
    "name": "report_audio_check",
    "description": "Report whether the candidate confirmed they can hear you clearly during the audio check.",
    "parameters": {
        "type": "object",
        "properties": {
            "candidate_confirmed": {
                "type": "boolean",
                "description": "true if the candidate clearly confirmed they can hear you, false otherwise",
            }
        },
        "required": ["candidate_confirmed"],
    },
}

QUESTION_STARTED_TOOL_SCHEMA = {
    "type": "function",
    "name": "question_started",
    "description": "Call the instant you begin asking one of the four approved main questions (not for follow-ups).",
    "parameters": {
        "type": "object",
        "properties": {"question_id": {"type": "string", "description": "e.g. q1, q2, q3, q4"}},
        "required": ["question_id"],
    },
}

QUESTION_COMPLETED_TOOL_SCHEMA = {
    "type": "function",
    "name": "question_completed",
    "description": "Call once you are done with a question (after at most one follow-up) and moving on.",
    "parameters": {
        "type": "object",
        "properties": {
            "question_id": {"type": "string"},
            "follow_up_used": {"type": "boolean"},
        },
        "required": ["question_id", "follow_up_used"],
    },
}


def _fire_and_forget(coro, label: str):
    """Postgres writes on the transcript/event hot path shouldn't block the
    realtime audio loop — scheduled as background tasks, errors logged but
    never allowed to interrupt the call itself."""
    async def _wrapped():
        try:
            await coro
        except Exception as e:
            logger.warning(f"Background write failed ({label}): {e}")
    asyncio.create_task(_wrapped())


async def handle_voice_bridge_ws(websocket, details: dict, end_call_callback=None):
    logger.info("Browser audio engine connected to routing matrix.")
    ai_responding = False
    pending_function_calls = {}
    end_call_event = asyncio.Event()

    # Module 2 mode is opt-in per-call: only active when the worker passed a
    # frozen assessment-plan snapshot (see app/worker/interview_worker.py,
    # app/services/ai_interview_service.py). Any other/older caller gets the
    # exact previous behavior, unaffected by anything below.
    module2_mode = bool(details.get("assessment_plan_snapshot"))
    redis_interview_id = details.get("interview_id")
    pg_session_id = details.get("pg_session_id")

    audio_check_attempts = 0
    audio_check_passed = False
    current_question_id = {"value": None}
    interview_started_event = asyncio.Event()
    if not module2_mode:
        interview_started_event.set()  # legacy behavior: timer starts immediately, as before

    state_machine = None
    ai_interview_repo = None
    if module2_mode and redis_interview_id:
        from app.state import machine as state_machine  # local import: keeps this module importable without Postgres/Redis configured for legacy callers
        from app.repositories import ai_interview_repo

    async def _transition(to_state: str, reason: str):
        if not (module2_mode and redis_interview_id and state_machine):
            return
        try:
            await state_machine.transition(redis_interview_id, state_machine.InterviewState(to_state), reason=reason)
        except Exception as e:
            logger.warning(f"State transition to {to_state} failed: {e}")

    def _log_event(event_type: str, event_data: dict | None = None):
        if module2_mode and pg_session_id and ai_interview_repo:
            _fire_and_forget(ai_interview_repo.insert_event(pg_session_id, event_type, event_data), event_type)

    session_logger = TranscriptSessionLogger(
        candidate_name=details.get("candidate", "Unknown"),
        position_title=details.get("title") or details.get("position_title", "Technical Role"),
        assessment_plan=details.get("assessment_plan") or details.get("assessment_plan_snapshot"),
        job_description=details.get("job_description"),
    )

    base_url = settings.realtimeapi_azure_openai_service_endpoint.replace("https://", "").replace("http://", "").strip("/")
    url = f"wss://{base_url}/openai/v1/realtime?model={settings.realtimeapi_azure_openai_deployment_model_name}"
    headers = {"api-key": settings.realtimeapi_azure_openai_service_key}

    try:
        async with websockets.connect(url, additional_headers=headers) as openai_ws:
            initial_instructions = build_audio_check_instructions(details) if module2_mode else build_instructions(details)
            tools = [END_CALL_TOOL_SCHEMA]
            if module2_mode:
                tools += [REPORT_AUDIO_CHECK_TOOL_SCHEMA, QUESTION_STARTED_TOOL_SCHEMA, QUESTION_COMPLETED_TOOL_SCHEMA]

            await openai_ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "instructions": initial_instructions,
                    "tools": tools,
                    "audio": {
                        "input": {
                            "transcription": {"model": "whisper-1", "language": "en"},
                            "format": {"type": "audio/pcm", "rate": 24000},
                            "turn_detection": {"type": "server_vad", "silence_duration_ms": 3000}
                        },
                        "output": {
                            "voice": "shimmer",
                            "format": {"type": "audio/pcm", "rate": 24000}
                        }
                    }
                }
            }))

            greeted = False
            candidate_video_on = True
            video_prompt_sent = False
            # Tracks "waiting for the candidate to respond" for the silence
            # watchdog below: set to a timestamp whenever the AI finishes
            # talking, cleared once the candidate actually starts speaking.
            waiting_since = {"value": None}
            silence_nudge_sent = False

            async def prompt_video_on_if_still_off():
                nonlocal video_prompt_sent
                # Grace period so a brief camera glitch/toggle doesn't trigger
                # a prompt -- only nag if it's actually been off a few seconds.
                await asyncio.sleep(8)
                if candidate_video_on or video_prompt_sent:
                    return
                video_prompt_sent = True
                # Only updates the standing instructions -- deliberately does
                # NOT force a response.create here. This can fire at any point
                # in a live conversation (candidate mid-turn, about to speak,
                # etc.), unlike the other response.create call sites in this
                # file (greeting, audio-check retries, hard time cutoff),
                # which only ever run when no candidate turn is in flight. An
                # explicit response.create here would race with the server's
                # own VAD-triggered auto-response for whatever the candidate
                # is saying, producing two overlapping responses whose audio
                # gets interleaved into one continuous playback stream on the
                # client (heard as doubled/echoing audio), and whichever one
                # is "active" when the candidate next speaks gets cancelled,
                # clearing their input_audio_buffer and dropping what they
                # just said. Letting the model pick this up on its own next
                # natural turn avoids all of that -- matches how the periodic
                # (non-final) TIME STATUS updates below are handled.
                base_instructions = build_module2_instructions(details) if module2_mode else build_instructions(details)
                await openai_ws.send(json.dumps({
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "instructions": base_instructions + (
                            "\n\nNOTE: The candidate's camera appears to be off. Politely ask them "
                            "to turn on their video before continuing, then proceed once they do "
                            "(or once they say they'd rather keep it off)."
                        ),
                    }
                }))

            async def handle_openai_responses():
                nonlocal ai_responding, audio_check_attempts, audio_check_passed, silence_nudge_sent
                try:
                    async for response in openai_ws:
                        data = json.loads(response)
                        event_type = data.get("type")

                        if event_type == "conversation.item.input_audio_transcription.completed":
                            candidate_text = data.get('transcript', '').strip()
                            if candidate_text:
                                session_logger.log_turn("CANDIDATE", candidate_text)
                                if module2_mode and pg_session_id and ai_interview_repo:
                                    _fire_and_forget(
                                        ai_interview_repo.insert_transcript_turn(
                                            pg_session_id, "CANDIDATE", candidate_text,
                                            question_id=current_question_id["value"],
                                        ),
                                        "transcript:candidate",
                                    )

                        elif event_type == "response.output_audio_transcript.done":
                            ai_text = data.get('transcript', '').strip()
                            if ai_text:
                                session_logger.log_turn("AI_INTERVIEWER", ai_text)
                                if module2_mode and pg_session_id and ai_interview_repo:
                                    _fire_and_forget(
                                        ai_interview_repo.insert_transcript_turn(
                                            pg_session_id, "AI_INTERVIEWER", ai_text,
                                            question_id=current_question_id["value"],
                                        ),
                                        "transcript:ai",
                                    )

                        elif event_type == "response.output_audio.delta":
                            ai_responding = True
                            audio_chunk = data.get("delta")
                            if audio_chunk:
                                await websocket.send(base64.b64decode(audio_chunk))

                        elif event_type == "response.output_item.added":
                            item = data.get("item", {})
                            if item.get("type") == "function_call":
                                pending_function_calls[item.get("call_id")] = item.get("name")

                        elif event_type == "response.function_call_arguments.done":
                            call_id = data.get("call_id")
                            function_name = pending_function_calls.pop(call_id, None)

                            async def _tool_output(output: dict):
                                await openai_ws.send(json.dumps({
                                    "type": "conversation.item.create",
                                    "item": {
                                        "type": "function_call_output",
                                        "call_id": call_id,
                                        "output": json.dumps(output),
                                    }
                                }))

                            if function_name == "end_call":
                                try:
                                    args = json.loads(data.get("arguments", "{}"))
                                except json.JSONDecodeError:
                                    args = {}
                                logger.info(f"Model requested end_call: {args.get('reason', 'no reason given')}")
                                await _tool_output({"status": "ending_call"})
                                end_call_event.set()

                            elif function_name == "report_audio_check" and module2_mode:
                                try:
                                    args = json.loads(data.get("arguments", "{}"))
                                except json.JSONDecodeError:
                                    args = {}
                                confirmed = bool(args.get("candidate_confirmed"))
                                audio_check_attempts += 1
                                await _tool_output({"status": "recorded"})

                                if module2_mode and pg_session_id and ai_interview_repo:
                                    _fire_and_forget(
                                        ai_interview_repo.record_audio_check_attempt(pg_session_id, confirmed),
                                        "audio_check_attempt",
                                    )

                                if confirmed:
                                    audio_check_passed = True
                                    logger.info("Audio check passed.")
                                    _log_event("AUDIO_CHECK_PASSED")
                                    await _transition("INTERVIEWING", "audio check confirmed by candidate")
                                    if module2_mode and pg_session_id and ai_interview_repo:
                                        _fire_and_forget(ai_interview_repo.mark_session_started(pg_session_id), "session_started")
                                    interview_started_event.set()  # timer begins counting only now
                                    await openai_ws.send(json.dumps({
                                        "type": "session.update",
                                        "session": {"type": "realtime", "instructions": build_module2_instructions(details)}
                                    }))
                                    await openai_ws.send(json.dumps({"type": "response.create"}))
                                elif audio_check_attempts < MAX_AUDIO_CHECK_ATTEMPTS:
                                    logger.warning(f"Audio check failed (attempt {audio_check_attempts}); retrying.")
                                    _log_event("AUDIO_CHECK_FAILED", {"attempt": audio_check_attempts, "final": False})
                                    await openai_ws.send(json.dumps({
                                        "type": "session.update",
                                        "session": {"type": "realtime", "instructions": build_audio_retry_instructions(details)}
                                    }))
                                    await openai_ws.send(json.dumps({"type": "response.create"}))
                                else:
                                    logger.error("Audio check failed twice; marking TECHNICAL_FAILURE.")
                                    _log_event("AUDIO_CHECK_FAILED", {"attempt": audio_check_attempts, "final": True})
                                    _log_event("TECHNICAL_FAILURE", {"reason": "audio check failed twice"})
                                    await _transition("TECHNICAL_FAILURE", "audio check failed twice")
                                    if module2_mode and pg_session_id and ai_interview_repo:
                                        _fire_and_forget(
                                            ai_interview_repo.set_technical_failure(
                                                pg_session_id, "audio check failed twice", True
                                            ),
                                            "technical_failure",
                                        )
                                    await openai_ws.send(json.dumps({
                                        "type": "session.update",
                                        "session": {"type": "realtime", "instructions": build_audio_failure_instructions(details)}
                                    }))
                                    await openai_ws.send(json.dumps({"type": "response.create"}))
                                    # Safety net matching the existing hard-cutoff pattern:
                                    # force the hangup if the model doesn't call end_call itself.
                                    async def _force_hangup_if_stuck():
                                        try:
                                            await asyncio.wait_for(end_call_event.wait(), timeout=AUDIO_CHECK_FAILURE_GRACE_SECONDS)
                                        except asyncio.TimeoutError:
                                            logger.warning("Model didn't call end_call after audio-check failure; forcing hangup.")
                                            end_call_event.set()
                                    asyncio.create_task(_force_hangup_if_stuck())

                            elif function_name == "question_started" and module2_mode:
                                try:
                                    args = json.loads(data.get("arguments", "{}"))
                                except json.JSONDecodeError:
                                    args = {}
                                qid = args.get("question_id")
                                current_question_id["value"] = qid
                                logger.info(f"Question started: {qid}")
                                _log_event("QUESTION_STARTED", {"question_id": qid})
                                await _tool_output({"status": "recorded"})

                            elif function_name == "question_completed" and module2_mode:
                                try:
                                    args = json.loads(data.get("arguments", "{}"))
                                except json.JSONDecodeError:
                                    args = {}
                                qid = args.get("question_id")
                                follow_up_used = bool(args.get("follow_up_used"))
                                logger.info(f"Question completed: {qid} (follow_up_used={follow_up_used})")
                                _log_event("QUESTION_COMPLETED", {"question_id": qid, "follow_up_used": follow_up_used})
                                if module2_mode and pg_session_id and ai_interview_repo:
                                    _fire_and_forget(ai_interview_repo.increment_questions_completed(pg_session_id), "questions_completed")
                                current_question_id["value"] = None
                                await _tool_output({"status": "recorded"})

                                # The prompt asks the model to speak its
                                # transition + the next question (or closing
                                # remarks) in this SAME turn, but that's only
                                # a prompt-level instruction, not guaranteed --
                                # the model can still end its response right
                                # after the tool call with nothing more said.
                                # Since turn_detection is server_vad, nothing
                                # then ever prompts the model again (no new
                                # candidate speech to trigger VAD, and nobody
                                # asked a question for them to respond to),
                                # so the call stalls silently forever. Detect
                                # that: if the response ends without a new
                                # question_started call (and the call isn't
                                # already ending), force one more
                                # response.create so the model continues.
                                async def _continue_if_stalled():
                                    await asyncio.sleep(0.5)
                                    waited = 0.0
                                    while ai_responding and waited < 12.0:
                                        await asyncio.sleep(0.3)
                                        waited += 0.3
                                    if (
                                        not ai_responding
                                        and not end_call_event.is_set()
                                        and current_question_id["value"] is None
                                    ):
                                        logger.warning(
                                            "Model ended its turn after question_completed without "
                                            "continuing (no next question, no end_call); forcing continuation."
                                        )
                                        await openai_ws.send(json.dumps({"type": "response.create"}))
                                asyncio.create_task(_continue_if_stalled())

                            else:
                                # Unknown/irrelevant tool for this mode — still must
                                # acknowledge it or the model's turn stalls waiting
                                # for a function_call_output it'll never get.
                                await _tool_output({"status": "ignored"})

                        elif event_type == "response.done":
                            ai_responding = False
                            # Start (or restart) the "waiting for the
                            # candidate" clock for the silence watchdog below
                            # every time the AI finishes a turn -- including a
                            # silence nudge itself, so a candidate who's still
                            # not responding gets checked on again rather than
                            # only once for the whole call.
                            waiting_since["value"] = time.monotonic()
                            silence_nudge_sent = False

                        elif event_type == "error":
                            # Previously unhandled: a rejected/failed response
                            # (e.g. "conversation_already_has_active_response"
                            # from an explicit response.create racing a VAD-
                            # triggered one) silently fell through every elif
                            # above. If that happened while ai_responding was
                            # still True from an earlier response that never
                            # reached response.done, it stayed stuck True
                            # forever -- and the speaking_status handler below
                            # treats "ai_responding" as "there's a live
                            # response to cancel," clearing the candidate's
                            # input_audio_buffer every time they start talking
                            # from then on, which looks exactly like "the AI
                            # just stopped responding to anything." Always
                            # reset it here so one failed response can't
                            # permanently wedge the rest of the call.
                            logger.error(f"Realtime API error event: {data.get('error', data)}")
                            ai_responding = False
                except asyncio.CancelledError:
                    pass

            async def run_interview_timer():
                # Module 2: don't start counting down until the audio check
                # has actually passed and interview_started_event fires —
                # "the timer ... should start only after we hear a voice
                # from the candidate" (and, more specifically, after they've
                # confirmed the audio check). Legacy mode: event is already
                # set above, so this returns immediately, unchanged behavior.
                await interview_started_event.wait()

                total_seconds = details.get("duration_minutes", 30) * 60
                start = time.monotonic()
                while not end_call_event.is_set():
                    await asyncio.sleep(TIME_UPDATE_INTERVAL_SECONDS)
                    elapsed = time.monotonic() - start
                    remaining = total_seconds - elapsed

                    for _ in range(10):  # avoid clobbering a response mid-stream
                        if not ai_responding:
                            break
                        await asyncio.sleep(1)

                    base_instructions = build_module2_instructions(details) if module2_mode else build_instructions(details)
                    await openai_ws.send(json.dumps({
                        "type": "session.update",
                        "session": {
                            "type": "realtime",
                            "instructions": base_instructions + render_time_status(remaining)
                        }
                    }))

                    if remaining <= 0:
                        await openai_ws.send(json.dumps({"type": "response.create"}))
                        try:
                            await asyncio.wait_for(end_call_event.wait(), timeout=HARD_CUTOFF_GRACE_SECONDS)
                        except asyncio.TimeoutError:
                            logger.warning("Model didn't call end_call within grace period, forcing hangup.")
                            end_call_event.set()
                        return

            async def watch_for_forced_hangup():
                await end_call_event.wait()
                # The end_call tool call can be parsed before the same turn's
                # closing-remarks audio has finished streaming (function-call
                # and audio deltas are separate items within one response),
                # so wait for that response to actually finish (ai_responding
                # goes False on response.done) instead of guessing a fixed
                # delay long enough for any closing statement. Capped so a
                # missing/stuck response.done can't hang the call forever.
                waited = 0.0
                while ai_responding and waited < 15.0:
                    await asyncio.sleep(0.2)
                    waited += 0.2
                await asyncio.sleep(1.5)  # buffer for downstream Teams audio playback latency
                if module2_mode:
                    await _transition("CLOSING", "call ending")
                    _log_event("INTERVIEW_COMPLETED")
                if end_call_callback:
                    try:
                        await end_call_callback()
                    except Exception as e:
                        logger.error(f"Teams hangup callback failed: {e}")
                await websocket.close()

            async def watch_for_candidate_silence():
                nonlocal silence_nudge_sent
                # Server VAD only invokes the model on a speech-then-silence
                # transition -- if the candidate never speaks at all after a
                # question, there's no turn boundary to trigger the model, so
                # it can never "notice" the silence on its own no matter what
                # the prompt says. This polls real wall-clock time instead.
                while not end_call_event.is_set():
                    await asyncio.sleep(2)
                    started = waiting_since["value"]
                    if started is None or ai_responding or silence_nudge_sent:
                        continue
                    if time.monotonic() - started >= SILENCE_REPROMPT_SECONDS:
                        silence_nudge_sent = True
                        logger.info("Candidate has been silent for a while; nudging for a check-in.")
                        base_instructions = build_module2_instructions(details) if module2_mode else build_instructions(details)
                        await openai_ws.send(json.dumps({
                            "type": "session.update",
                            "session": {
                                "type": "realtime",
                                "instructions": base_instructions + (
                                    "\n\nNOTE: The candidate has been silent for a while since your "
                                    "last question. Gently check in -- ask if they're still there, or "
                                    "if they'd like you to repeat or rephrase the question -- then wait "
                                    "for their response."
                                ),
                            }
                        }))
                        await openai_ws.send(json.dumps({"type": "response.create"}))

            response_task = asyncio.create_task(handle_openai_responses())
            timer_task = asyncio.create_task(run_interview_timer())
            hangup_task = asyncio.create_task(watch_for_forced_hangup())
            silence_task = asyncio.create_task(watch_for_candidate_silence())

            try:
                async for message in websocket:
                    if isinstance(message, str):
                        data = json.loads(message)
                        if data.get("type") == "participant_joined":
                            if not greeted:
                                greeted = True
                                if module2_mode:
                                    _log_event("CANDIDATE_JOINED")
                                    await _transition("AUDIO_CHECK", "candidate speech detected; starting audio check")
                                await openai_ws.send(json.dumps({"type": "response.create"}))
                        elif data.get("type") == "speaking_status" and data.get("speaking", False):
                            # The candidate is actually talking now -- stop
                            # waiting/watching for silence until the AI's next turn.
                            waiting_since["value"] = None
                            silence_nudge_sent = False
                            if ai_responding:
                                await openai_ws.send(json.dumps({"type": "response.cancel"}))
                                await websocket.send(json.dumps({"type": "stop_audio"}))
                                ai_responding = False
                                await openai_ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                        elif data.get("type") == "video_status":
                            enabled = bool(data.get("enabled"))
                            if enabled:
                                candidate_video_on = True
                                video_prompt_sent = False
                            elif candidate_video_on:
                                candidate_video_on = False
                                asyncio.create_task(prompt_video_on_if_still_off())

                    elif isinstance(message, bytes):
                        await openai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(message).decode("utf-8")
                        }))
            finally:
                response_task.cancel()
                timer_task.cancel()
                hangup_task.cancel()
                silence_task.cancel()
                await asyncio.gather(response_task, timer_task, hangup_task, silence_task, return_exceptions=True)
                # Local-disk transcript persistence (save_session_to_disk) was removed --
                # doesn't survive a container restart/redeploy the way a DB row does.
                # Module 2 sessions get the full transcript written to
                # ai_interview_sessions.full_transcript_json instead; a legacy (non-module2)
                # caller has no pg_session_id/ai_interview_sessions row to attach one to, so
                # its transcript now only exists in-memory for the duration of the call --
                # that legacy path predates Module 2 and isn't part of the current pipeline.
                if module2_mode and pg_session_id and ai_interview_repo:
                    _fire_and_forget(
                        ai_interview_repo.save_full_transcript(pg_session_id, session_logger.build_payload()),
                        "full_transcript",
                    )
                    try:
                        await ai_interview_repo.mark_session_ended(pg_session_id)
                    except Exception as e:
                        logger.warning(f"Could not mark session ended in Postgres: {e}")

    except Exception as e:
        logger.error(f"Realtime WebSocket Processing Exception: {e}")
