# AI Interviewer — Architecture & API Reference

This document explains the whole system: every API endpoint, every major
module, and how an interview flows end-to-end from scheduling to completion.

## 1. What this system does

An AI bot joins a Microsoft Teams meeting as a participant, conducts a
spoken technical interview with a candidate using a real-time speech-to-
speech AI model (Amazon Nova Sonic via AWS Bedrock), asks a fixed set of
approved questions, records the conversation transcript, and ends the call —
all without a human interviewer present. A second "admitter" bot (using a
real signed-in Teams identity) joins first to let the anonymous AI
interviewer bot past the meeting lobby.

## 2. Directory structure

```
app/
  api/            FastAPI routes + Pydantic request/response models
  bot/            Playwright browser automation (join/admit/leave Teams, fake mic/camera)
    scripts/      JS injected into the Teams page (interceptor, audio bridge, stealth)
  db/             Postgres connection pool
  infra/          Redis client + distributed lock
  integrations/   Microsoft Graph API client (meeting creation)
  realtime/       The live AI voice pipeline (Nova Sonic) + transcript logging
  repositories/   All Postgres SQL (one file, 4 tables)
  scheduling/     The "when does this interview start" queue + capacity limits
  services/       Business logic orchestrating everything above
  state/          The Redis-backed interview state machine
  worker/         Per-interview OS subprocess entrypoint + supervisor
```

## 3. The big picture: one interview, start to finish

1. **Schedule** — `POST /api/v1/ai-interviews` creates a Postgres row, reserves
   the shared bot account, creates a real Teams meeting via Microsoft Graph,
   and queues the interview in Redis for a future start time.
2. **Dispatch** — a background loop (`run_scheduler_loop`) polls that Redis
   queue every 15s; when an interview is due soon, it spawns a **brand new
   OS process** (`python -m app.worker.interview_worker`) to run it.
3. **Join** — that worker process launches **one shared Chrome browser**
   with two tabs/contexts: the **admitter** (a real signed-in Teams identity)
   joins first, optionally starts cloud recording, then admits the
   **guest** (the anonymous AI interviewer bot) from the lobby once it
   appears, then leaves.
4. **Interview** — the guest bot's browser page has fake mic/camera
   hardware injected via JavaScript, bridging audio to/from a local Python
   WebSocket, which is itself bridged to Amazon Nova Sonic's real-time
   speech-to-speech API. The AI asks the approved questions, listens, and
   only re-asks if a response is unclear.
5. **Wrap up** — when time runs out (or the model decides to end), the AI
   gives a closing statement, the bot leaves the meeting, and the full
   transcript + question-by-question progress gets written to Postgres.
6. **Supervise** — a second background loop (`run_supervisor_loop`) enforces
   a hard timeout per interview and cleans up finished/crashed processes.

Redis is the fast, ephemeral coordination layer between the one long-running
API process and the many short-lived worker processes; Postgres is the
permanent record (bookings, transcripts).

## 4. API Reference

All routes return JSON. Errors use standard FastAPI `HTTPException` status
codes (400/404/409/500/501 as noted).

### Legacy/Module-1 flow — mounted at `/api` (`app/api/routes.py`)

| Method & Path | Body | Description |
|---|---|---|
| `POST /api/v1/interviews/start` | `InterviewStartRequest` (`meeting_url`, `candidate_name`, `position_title`, `job_description`, `resume_text`, `duration_minutes`, `join_mode`, `scheduled_time`, `assessment_plan`) | Starts (or schedules) an interview directly from a request body — no Postgres row, no Graph meeting creation (the caller already has a `meeting_url`). Initializes Redis state and queues it via the scheduler. Returns `{status, interview_id, message, candidate, meeting_url, join_mode}`. |
| `GET /api/v1/interviews/{interview_id}/status` | — | Returns the raw Redis state hash for that interview, or 404. |

### Module 2 flow — mounted at `/api/v1/ai-interviews` (`app/api/ai_interviews_routes.py`)

| Method & Path | Body | Description |
|---|---|---|
| `POST /api/v1/ai-interviews` | `ScheduleAiInterviewRequest` (`assessment_plan_id`, `assessment_plan_version`, `scheduled_start`, `timezone`, `allow_past_start`, `existing_meeting_url`) | The real scheduling flow: loads the assessment plan, reserves the bot account, creates a Teams meeting via Graph, persists everything to Postgres, and queues it in Redis. See §5 for the full step-by-step. Idempotent on (plan id, version). Errors: 400 past start time, 404 plan not found, 409 bot account busy, 501 if a plan-loader stub isn't implemented. |
| `GET /api/v1/ai-interviews/{id}` | — | Returns the full `ai_interviews` Postgres row, or 404. |
| `PATCH /api/v1/ai-interviews/{id}/schedule` | `RescheduleAiInterviewRequest` (`scheduled_start`, `timezone`) | Moves an interview to a new time: best-effort updates the Graph meeting, updates Postgres, re-queues it in Redis at the new time. |
| `POST /api/v1/ai-interviews/{id}/cancel` | `CancelAiInterviewRequest` (`reason`) | Cancels: best-effort deletes the Graph meeting, releases the bot account reservation, removes it from the Redis queue, transitions state to `CANCELLED`. |
| `POST /api/v1/ai-interviews/{id}/start?force_restart=bool` | — (dev/testing only) | Forces immediate dispatch, bypassing the scheduler's normal timing. `force_restart=true` resets a non-`SCHEDULED` interview back to scratch first. |

### Internal — mounted at `/internal/v1` (`app/api/internal_routes.py`, no `/api` prefix)

| Method & Path | Body | Description |
|---|---|---|
| `POST /internal/v1/interview-sessions/{session_id}/finalise` | — | Idempotent finalisation hook: marks the session finalised in Postgres and publishes `{"interview_session_id": ...}` to the Redis pub/sub channel `interview_session_finalised`, presumably for an external evaluator/scoring service to pick up. |

### Misc

- `GET /health` → `{"status": "operational"}` — liveness check, no auth.

## 5. Service layer — `app/services/ai_interview_service.py`

This is the only module that talks to the repository layer, the meeting
provider, and the Redis state machine/scheduler together — all orchestration
lives here.

**`schedule_ai_interview(...)`** — the main flow behind `POST /ai-interviews`:
1. Reject past start times (unless `allow_past_start`).
2. Idempotency check: if a non-terminal row already exists for this
   (plan id, version), verify it's still actually alive via Redis
   (`_resolve_stale_existing`) and return it unchanged if so — otherwise
   mark it `FAILED` and proceed with a fresh attempt.
3. `load_assessment_plan(...)` — reads the frozen plan from Postgres
   (`ai_assessment_plans`: `plan_json`, `job_snapshot_json`,
   `candidate_summary_snapshot_json`) and returns a snapshot dict with
   `candidate_name`, `position_title`, `duration_minutes`, `competencies`,
   `questions`, `job_snapshot`, `candidate_summary_snapshot`.
4. Create the `ai_interviews` Postgres row with that snapshot frozen into it.
5. Reserve the shared bot account (20-minute window) — on conflict, mark
   the row `FAILED` and raise `BotAccountBusyError` (→ 409).
6. Create the real Teams meeting via `meeting_provider.create_meeting(...)`
   (Microsoft Graph) — on failure, mark `MEETING_CREATION_FAILED` and
   release the bot account reservation.
7. Persist the meeting URL/ID; create the `ai_interview_sessions` row
   (attempt #1).
8. Initialize Redis state (`state_machine.init_state`) and queue it in the
   scheduler (`schedule_interview`), with `join_mode` always `"guest"` for
   this flow.

Other functions: `get_ai_interview` (thin read), `reschedule_ai_interview`
(update Graph + Postgres + re-queue in Redis), `cancel_ai_interview`
(best-effort Graph delete + release reservation + Redis cleanup + Postgres
update), `start_ai_interview_dev` (dev-only: force-dispatch immediately,
bypassing the scheduler's timing).

## 6. Meeting creation — Graph integration

`app/services/meeting_provider.py` defines a `MeetingProvider` abstraction
(`create_meeting`/`update_meeting`/`cancel_meeting`) with one implementation,
`TeamsMeetingProvider`, which wraps `app/integrations/graph_client.py`'s
blocking `requests`-based calls in `asyncio.to_thread(...)` so they don't
stall the event loop.

`graph_client.py` authenticates via **MSAL client-credentials flow** (app-only
auth, no signed-in user — `TEAMS_CLIENT_ID`/`TEAMS_TENANT_ID`/
`TEAMS_CLIENT_SECRET`), caching the token until ~60s before expiry. Because
this is app-only auth, Graph's `onlineMeetings` endpoints require the
organizer's **Azure AD object ID (a GUID)**, not an email — `TEAMS_USER_OBJECT_ID`.
`create_online_meeting` sets `lobbyBypassSettings.scope = "everyone"` so the
admitter, guest bot, and real candidate all skip the lobby automatically
(though in practice the guest still shows in "waiting to be admitted" for an
anonymous join — the admitter bot handles that).

## 7. Bot & browser automation — `app/bot/`

- **`login_setup.py`** — run once by hand (`python -m app.bot.login_setup`):
  opens a real headed Chrome, a human signs in to the bot's Teams account
  (including MFA), then the script saves the session (cookies/localStorage)
  to `app/bot/admitter_state.json` via Playwright's `storage_state()`. This
  file is what lets the automated admitter join already authenticated.
- **`engine.py`** — orchestrates the actual browser session for an
  interview: `launch_admitter_and_guest()` opens **one shared Chromium
  process** with two browser contexts (tabs) — the admitter (loaded from
  `admitter_state.json`) and the guest (fresh/anonymous). The admitter joins
  first, optionally starts Teams' cloud recording, admits the guest once it
  appears in the lobby (confirmed via the People panel, not a lobby-count
  heuristic), then leaves — Teams records at the meeting level, so recording
  continues after the admitter leaves. The guest tab runs the full
  interview: injects the fake-hardware/audio-bridge scripts, joins, and
  bridges its audio to the realtime AI pipeline until the call ends.
- **`teams.py`** — all the actual Teams web UI automation: the pre-join flow
  (dismissing native-app prompts, entering the guest display name, turning
  the camera on, clicking Join), admitting a waiting participant by name,
  starting cloud recording via the "More actions" menu, and leaving the call.
  Every UI-dependent step is best-effort with fallback selectors and debug
  screenshots on failure, since Teams' web UI isn't a stable public API.
- **`scripts/interceptor.js`** — injected before any other page script runs.
  Fakes a virtual microphone (silent `AudioContext`) and virtual camera (a
  canvas, continuously drawing an avatar image, matched to Teams' exact
  requested resolution/framerate) so `getUserMedia`/`enumerateDevices`
  succeed in a headless browser with no real hardware.
- **`scripts/custom_audio_payload.js`** — the audio bridge between the Teams
  page and the Python backend over a local WebSocket. Plays AI speech
  through the virtual mic; captures and mixes all remote participants'
  audio (excluding the bot's own voice, to prevent echo), cleans it with a
  filter chain, runs simple energy-based voice-activity detection, and
  streams PCM16 audio + `speaking_status`/`participant_joined`/`video_status`
  control messages to Python.
- **`scripts/stealth_injection.js`** — standard automation-detection evasion
  (hides `navigator.webdriver`, fakes plugins/vendor, etc.).

## 8. The realtime AI voice pipeline — `app/realtime/connection.py`

`handle_voice_bridge_ws(websocket, details, end_call_callback)` is the heart
of the live interview. One instance runs per interview, bridging the
browser's local WebSocket to a Bedrock bidirectional stream (Amazon Nova
Sonic). Responsibilities:

- Builds the system prompt (`build_module2_instructions`): the exact
  approved questions in priority order, told to ask them one at a time with
  **no follow-up probing** — only re-ask if a response is unclear, cut off,
  or off-topic.
- Runs an audio-check phase first (module 2 mode): "can you hear me?" with
  up to 4 attempts before giving up.
- Maintains a persistent audio content stream to Nova Sonic for the whole
  call; tool calls (`end_call`, `report_audio_check`, `question_started`,
  `question_completed`) are dispatched as they arrive and their results sent
  back inline.
- Several background watchdogs: a timer that injects "time remaining"
  guidance and (at time-up) waits for the candidate to actually finish
  speaking before forcing a closing statement; a silence watchdog that
  nudges the model to check in if the candidate goes quiet; a camera-off
  watchdog; and a **session-rotation** watchdog that proactively reconnects
  to a fresh Bedrock session before Nova Sonic's ~8-minute connection cap,
  carrying forward a short progress note (not the full transcript).
- Logs every transcript turn (candidate + AI) to Postgres via
  `TranscriptSessionLogger` and `ai_interview_repo`, and drives the Redis
  state machine through `AUDIO_CHECK` → `INTERVIEWING` → `CLOSING`.

`TranscriptSessionLogger` (`app/realtime/transcript.py`) just accumulates
`{timestamp, speaker, text}` turns and builds the final JSON payload stored
in `ai_interview_sessions.full_transcript_json`.

## 9. State management — `app/state/machine.py`

A Redis-backed state machine, one instance per interview, built from 3 keys:
`interview:{id}:state` (current stage + metadata, a hash), `interview:{id}:history`
(an append-only log stream of every transition, for debugging), and
`interview:{id}:heartbeat` (a 45s-TTL key refreshed every ~15s by the active
worker — how the system detects a crashed process).

States: `SCHEDULED → PREPARING → HOST_JOINING → INTERVIEWER_JOINING →
WAITING_FOR_CANDIDATE → AUDIO_CHECK → INTERVIEWING → CLOSING →
FINALISING_TRANSCRIPT → COMPLETED`, or any of 6 terminal failure states
(`MEETING_CREATION_FAILED`, `BOT_JOIN_FAILED`, `CANDIDATE_NO_SHOW`,
`TECHNICAL_FAILURE`, `CANCELLED`, `FAILED`). Transitions are validated
against an explicit adjacency map — an illegal jump raises instead of
silently corrupting state.

## 10. Scheduling & worker pool — `app/scheduling/`, `app/worker/`

- **`scheduler.py`** — `run_scheduler_loop()` polls a Redis sorted set
  (`capacity:schedule`, scored by intended start time) every 15s for
  anything due within `prewarm_lead_seconds` (180s), checks capacity, and
  calls `start_worker(...)`.
- **`capacity.py`** — a Redis set (`capacity:active_set`) tracking currently-
  running interviews, capped at `max_concurrent_interviews` (default 6).
  `reconcile_active_set()` self-heals this after a crash/restart by checking
  each tracked interview's heartbeat.
- **`supervisor.py`** — `start_worker()` spawns the actual
  `python -m app.worker.interview_worker` OS subprocess and tracks its
  handle + a hard deadline. `run_supervisor_loop()` polls every 10s: reaps
  (cleans up bookkeeping for) processes that exited on their own, and
  actively kills (`terminate()` then `kill()`) any process that's overrun
  its hard deadline.
- **`interview_worker.py`** — the subprocess entrypoint. Drives the Redis
  state machine through the join phases, delegates the actual Teams/audio
  work to `app.bot.engine`, and mirrors the final `COMPLETED`/`FAILED`
  status back into Postgres (`ai_interviews.status`) when it exits.

Both the scheduler and supervisor loops run as `asyncio` background tasks
inside the one FastAPI process (started in `app/main.py`'s startup event) —
not separate services.

## 11. The distributed admitter lock — `app/infra/lock.py`

Since only one real signed-in Teams identity exists, and interviews run as
separate OS processes (not just threads), an `asyncio.Lock` can't coordinate
across them. `held_lock()` uses an atomic Redis `SET NX PX` (non-blocking —
raises immediately if already held) with a renewable TTL, so a crashed
holder's lock expires on its own (≤60s) instead of staying stuck forever.

## 12. Database — Postgres tables (`app/repositories/ai_interview_repo.py`)

| Table | Purpose |
|---|---|
| `bot_account_reservations` | Ensures only one interview uses the shared bot account at a time (partial unique index on `status='RESERVED'`). |
| `ai_interviews` | One row per booked interview: candidate/position info, the frozen assessment-plan snapshot, meeting details, overall lifecycle `status`. |
| `ai_interview_sessions` | One row per attempt (retries get session_number 2, 3...); tracks audio-check attempts, questions completed, and the full transcript JSON. |
| `interview_transcript_turns` | The real-time, turn-by-turn transcript, tagged by question id. |
| `interview_events` | Audit log of everything that happened (joins, admits, audio-check results, question progress, state transitions). |

Schema DDL lives in a separate plan document (not in-repo as a migration) —
must be run against Postgres by hand before first use.

## 13. Connection pooling

- **Postgres** (`app/db/postgres.py`): a single lazily-created `asyncpg.Pool`
  (`min_size=1, max_size=10`), shared process-wide.
- **Redis** (`app/infra/redis_client.py`): a single `redis.asyncio.Redis`
  client (`@lru_cache()`-memoized), internally pooling connections.

## 14. Deployment notes

- Single container/process handles the whole app (API + scheduler +
  supervisor loops) — but that same process also spawns every worker
  subprocess, which runs Playwright/Chromium, so everything lives together
  in one image/host for now. Running 2+ replicas risks double-dispatching
  the same interview (the scheduler's dispatch isn't fully atomic yet).
- `app/bot/admitter_state.json` (the signed-in Teams session) and `.env`
  (secrets) are both excluded from the Docker image build and git — they're
  meant to be supplied at container *runtime* (bind-mounted / `--env-file`),
  not baked in, so rebuilding the image for a code change never touches
  them and refreshing a login never requires a rebuild.
- Nova Sonic requires AWS credentials (via `AWS_ACCESS_KEY_ID`/
  `AWS_SECRET_ACCESS_KEY` for local dev, or an IAM role on EC2 — verify the
  experimental `aws_sdk_bedrock_runtime` SDK's credential resolver actually
  picks up an instance role before relying on it in production).
