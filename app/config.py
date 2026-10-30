from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache

class Settings(BaseSettings):
    # Realtime voice API: Amazon Nova Sonic via AWS Bedrock's bidirectional
    # streaming (InvokeModelWithBidirectionalStream) -- not a plain API key
    # like the previous Azure OpenAI Realtime setup. The current client uses
    # environment credentials; set these for local dev, or provide AWS
    # credentials in the worker process environment. An EC2 role is not
    # resolved automatically by this configuration.
    aws_region: str = "us-east-1"
    bedrock_model_id: str = "amazon.nova-sonic-v1:0"
    bedrock_voice_id: str = "matthew"
    # Nova Sonic USD rates per 1M tokens, used to compute cost_usd for the
    # ai_interviewer_llm_usage row. Defaults are AWS's Nova 2 Sonic rates;
    # update them here (or in .env) if AWS pricing or the model changes.
    nova_sonic_speech_in_per_m: float = 3.0
    nova_sonic_speech_out_per_m: float = 12.0
    nova_sonic_text_in_per_m: float = 0.319
    nova_sonic_text_out_per_m: float = 2.651
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_session_token: str = ""

    # Base Web Server Options
    bot_name: str = "Proxy AI Interviewer"
    websocket_host: str = "localhost"
    websocket_port: int = 8766  # fallback default for local single-process dev only

    # Production worker/state/capacity settings
    redis_url: str = "redis://localhost:6379/0"
    max_concurrent_interviews: int = 6
    prewarm_lead_seconds: int = 180
    # This is measured from worker *spawn* (before the admitter/guest join,
    # the lobby wait, and any audio-check retries -- all of which happen
    # before the real Q&A portion even begins), not from when the interview
    # actually starts. It needs to comfortably cover that join/admit/audio-
    # check overhead on top of duration_minutes, so the hard-kill stays a
    # last-resort backstop for genuine hangs and never fires while a normal
    # interview is still legitimately in progress -- ending a normal call is
    # the realtime API's own timer's job (run_interview_timer in
    # connection.py), not this one's.
    hard_kill_grace_seconds: int = 600

    # Postgres (durable ai_interviews / sessions / reservations / transcript / events)
    database_url: str = "postgresql://postgres:postgres@localhost:5432/ai_interviewer"

    # Microsoft Graph (meeting creation via the signed-in bot account) — these
    # already existed in .env for the app registration but were previously unused.
    teams_client_id: str = ""
    teams_tenant_id: str = ""
    teams_client_secret: str = ""
    teams_user_email: str = ""
    # Graph's onlineMeetings create/update/delete under APPLICATION permissions
    # (app-only auth, no signed-in user) requires the organizer's Azure AD
    # object ID (a GUID) in the URL path -- passing the email/UPN there fails
    # with "InvalidArgument: The userId in request URL is not a valid GUID.",
    # even though most other /users/{id} Graph endpoints accept either. This
    # app registration also lacks User.Read.All, so it can't resolve the email
    # to this GUID itself -- get it from Entra admin center (entra.microsoft.com
    # -> Users -> find the bot account -> "Object ID" field) and set it here.
    teams_user_object_id: str = ""

    bot_account_email: str = ""  # defaults to teams_user_email if unset

    # Browser launch: empty (default) uses the real installed Chrome via
    # channel="chrome" (the confirmed-working path). Set this to a chrome.exe
    # path to launch Playwright's own bundled Chromium instead -- an
    # experiment to see whether the native-app-launch-popup / policy fixes
    # still hold on plain Chromium, ahead of a Linux/Docker deploy where
    # bundled Chromium is far simpler to provision than real Chrome.
    browser_executable_path: str = ""

    # Set to false to run with a visible browser window (useful when
    # debugging the admitter/guest join flow locally); defaults to true
    # (headless) for normal/deployed operation.
    browser_headless: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

@lru_cache()
def get_settings() -> Settings:
    return Settings()