import asyncio
import base64
import functools
import logging
import shutil
import tempfile
from pathlib import Path
import sys
import playwright
import websockets
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from app.config import get_settings
from app.realtime.connection import handle_voice_bridge_ws
# IMPORT THE NEW LEAVE FUNCTION HERE
from app.bot.teams import do_teams_join, leave_teams_call, admit_from_lobby, start_recording, set_role
from app.infra.lock import held_lock, extend_lock

logger = logging.getLogger(__name__)
settings = get_settings()

BASE_DIR = Path(__file__).parent
SIGNED_IN_PROFILE_DIR = BASE_DIR / "browser_profile"
ADMITTER_STATE_FILE = BASE_DIR / "admitter_state.json"
AVATAR_IMAGE_FILE = BASE_DIR.parent / "realtime" / "image.png"


@functools.lru_cache(maxsize=1)
def _load_interceptor_js() -> str:
    """Reads interceptor.js and injects the avatar image (once -- both the
    file and the resulting string never change at runtime, hence the cache)
    as a base64 data URI in place of the "__AVATAR_DATA_URI__" placeholder,
    so the bot's fake outgoing camera shows this image instead of a plain
    black frame (see the matching drawImage code in interceptor.js)."""
    with open(BASE_DIR / "scripts" / "interceptor.js", "r", encoding="utf-8") as f:
        script = f.read()
    avatar_bytes = AVATAR_IMAGE_FILE.read_bytes()
    avatar_data_uri = f"data:image/png;base64,{base64.b64encode(avatar_bytes).decode('ascii')}"
    return script.replace("__AVATAR_DATA_URI__", avatar_data_uri)

_CONTEXT_KWARGS = dict(
    permissions=["camera", "microphone"],
    viewport={"width": 1440, "height": 900},
    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
    ignore_https_errors=True,
)

# The signed-in identity can only be in one meeting at a time. This is a
# Redis-backed distributed lock (not asyncio.Lock) because each interview now
# runs as its own OS process/worker, potentially on a different machine —
# an in-process lock would mean nothing across process boundaries.
ADMITTER_IDENTITY_LOCK = "admitter-identity:default"


def _browser_launch_channel_kwargs() -> dict:
    """channel="chrome" (real installed Chrome) is the confirmed-working
    default. When BROWSER_EXECUTABLE_PATH is set, launch Playwright's own
    bundled Chromium from that path instead -- lets this be swapped for a
    one-off test without touching the two call sites themselves."""
    if settings.browser_executable_path:
        logger.info(f"Using override browser executable: {settings.browser_executable_path}")
        return {"executable_path": settings.browser_executable_path}
    logger.info('Using channel="chrome" (real installed Chrome)')
    return {"channel": "chrome"}

# Disable bot detection by removing automation-revealing flags
BROWSER_ARGS = [
    "--autoplay-policy=no-user-gesture-required",
    # Headless Chrome has no real audio/video hardware, so without this,
    # Chrome's own internal device list (separate from our JS-level
    # enumerateDevices() override in interceptor.js) is empty, which can
    # affect WebRTC's own device-selection/permission plumbing before any
    # page JS ever runs. Harmless in headed mode too.
    "--use-fake-device-for-media-stream",
    # Chrome treats a headless page as a backgrounded/occluded tab and
    # throttles it accordingly (timers, and in some cases how quickly a
    # freshly-loaded SPA finishes attaching its event handlers) -- headed
    # mode never hits this since the window is actually visible/focused.
    # This is the likely cause of the intermittent "pressed Enter on the
    # Continue-on-this-browser button before its click handler was even
    # attached yet" race that only ever showed up in headless mode.
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-popup-blocking",
    "--disable-translate",
    "--disable-sync",
    "--disable-features=LocalNetworkAccessChecks,PrivateNetworkAccessSendPreflights,PrivateNetworkAccessRespectPreflightResults",
]

_EXTERNAL_APP_SCHEMES = ("msteams:", "msteams-x-c2:", "ms-teams:", "teams:")


def force_teams_web_join(meeting_url: str) -> str:
    """Appends webjoin=true to a Teams meeting URL. UNVERIFIED: this isn't a
    parameter we've confirmed is real/documented by Microsoft -- it's worth
    testing for free since an unrecognized query param is very unlikely to
    break anything, but don't treat its presence as a guaranteed fix for the
    native "Open Microsoft Teams?" prompt. Adding it is not mutually
    exclusive with the registry URLBlocklist policy -- keep pursuing that
    too."""
    if "webjoin=true" in meeting_url.lower():
        return meeting_url
    separator = "&" if "?" in meeting_url else "?"
    return f"{meeting_url}{separator}webjoin=true"


async def _block_external_app_launch(context, log: logging.Logger) -> None:
    """Teams' join page tries to hand off to the native desktop app via a
    custom URL scheme (msteams://...), which triggers Chrome's own native
    "Open Microsoft Teams?" dialog -- browser-chrome UI, not page content,
    so it never shows up in a page.screenshot() and isn't something
    page.on("dialog") (which is for JS alert/confirm only) can see or
    dismiss either. In headed mode a human clicks Cancel on it without
    thinking about it; headless has no one to do that, and if it's truly
    OS-modal it can silently swallow keyboard/click input meant for the
    actual page underneath -- a very plausible explanation for input that
    looks like it should have worked but had no effect. Aborting the
    navigation before Chrome ever hands it to the OS avoids the dialog
    (and this whole class of doubt) entirely, in both headed and headless."""
    async def _abort(route):
        log.info(f"Blocked external app launch attempt: {route.request.url}")
        await route.abort()

    for scheme in _EXTERNAL_APP_SCHEMES:
        await context.route(f"{scheme}**", _abort)

async def launch_bot(
    meeting_url: str,
    details: dict,
    join_mode: str = "signed_in",
    admitter_ready: asyncio.Event | None = None,
):
    stop_event = asyncio.Event()

    # Tag every log line from this session (including inside teams.py's
    # helpers) as "guest" or "signed_in" so concurrent sessions in the
    # terminal are distinguishable. set_role() is context-local per asyncio
    # task, so it never leaks into the admitter's or another guest's task.
    role = "guest" if join_mode in ("guest", "guest_only") else "signed_in"
    set_role(role)
    log = logging.getLogger(f"app.bot.{role}")

    if join_mode == "guest" and admitter_ready is not None:
        log.info("Guest bot waiting for admitter to join the meeting first.")
        try:
            # 150s, not 60s: in the Docker/Linux path (Xvfb + the unbranded
            # chromium package), the admitter's own join -- browser-selection
            # prompt, navigation, in-meeting confirmation -- has been observed
            # taking 90-100+ seconds end to end, well past a 60s budget, even
            # though it does genuinely succeed. Native Windows Chrome was
            # fast enough that 60s never got tested here before.
            await asyncio.wait_for(admitter_ready.wait(), timeout=150)
            log.info("Admitter is ready; starting guest bot join.")
        except asyncio.TimeoutError:
            log.error("Admitter did not become ready within 150 seconds; guest bot will not join.")
            return

    guest_profile_dir = None
    if join_mode in ("guest", "guest_only"):
        # Fresh, throwaway profile per session: no shared Microsoft identity, so
        # multiple meetings can run concurrently without conflicting with each
        # other. The join is anonymous, so admission still depends on either a
        # signed-in admitter (join_mode="guest") or the tenant's/organizer's
        # lobby policy admitting it directly (join_mode="guest_only").
        guest_profile_dir = Path(tempfile.mkdtemp(prefix="teams_guest_profile_"))
        profile_dir = guest_profile_dir
        log.info(f"Guest join mode ({join_mode}): using throwaway profile at {profile_dir}")
    else:
        profile_dir = SIGNED_IN_PROFILE_DIR
        log.info(f"Signed-in join mode: using persistent profile at {profile_dir}")

    # NEW: Create a mutable reference to store the Playwright page once it opens
    page_ref = {"page": None}

    # NEW: Define the callback that the Realtime API will trigger when time is up
    async def end_call_action():
        if page_ref["page"]:
            await leave_teams_call(page_ref["page"])
        else:
            log.error("end_call_action triggered, but browser page is not active.")

    # MODIFIED: Pass the callback into your websocket handler
    # Bind to port 0 so the OS assigns a free port — each interview now runs
    # as its own process, so there's no coordination needed and no risk of
    # colliding with another concurrently-running interview's bridge.
    bound_port = {"value": None}

    async def start_server():
        async with websockets.serve(
            lambda ws: handle_voice_bridge_ws(ws, details, end_call_callback=end_call_action),
            settings.websocket_host,
            0,
        ) as server:
            bound_port["value"] = server.sockets[0].getsockname()[1]
            await stop_event.wait()

    server_task = asyncio.create_task(start_server())
    while bound_port["value"] is None:
        await asyncio.sleep(0.05)
    log.info(f"Voice bridge bound to port {bound_port['value']}")

    interceptor_js = _load_interceptor_js()
    with open(BASE_DIR / "scripts" / "custom_audio_payload.js", "r", encoding="utf-8") as f:
        custom_audio_js = f.read()
    with open(BASE_DIR / "scripts" / "stealth_injection.js", "r", encoding="utf-8") as f:
        stealth_js = f.read()

    log.info(f"Python: {sys.executable}")
    log.info(f"Playwright module: {playwright.__file__}")

    async with async_playwright() as p:
        log.info(f"Chromium executable: {p.chromium.executable_path}")

        context = await p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=settings.browser_headless,
            **_browser_launch_channel_kwargs(),
            args=BROWSER_ARGS,
            permissions=["camera", "microphone"],
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
            },
            ignore_https_errors=True,
        )
        await _block_external_app_launch(context, log)

        page = context.pages[0] if context.pages else await context.new_page()

        # NEW: Assign the active page to our reference so the callback can use it
        page_ref["page"] = page

        page.on("console", lambda msg: log.info(f"[BROWSER] {msg.text}"))
        page.on("pageerror", lambda exc: log.error(f"[BROWSER ERROR] {exc}"))
        # Low-confidence extra layer: page.on("dialog") is for JS alert/
        # confirm/prompt/beforeunload, a different CDP mechanism than
        # Chrome's native external-protocol-launch prompt -- this probably
        # won't fire for that dialog, but costs nothing to have in place in
        # case some Chrome version does route it through here.
        page.on("dialog", lambda dialog: asyncio.create_task(dialog.dismiss()))

        try:
            await Stealth().apply_stealth_async(page)
            await page.add_init_script(stealth_js)
            await page.add_init_script(interceptor_js)
            # force_teams_web_join()'s webjoin=true param is disabled for now --
            # suspected of causing joins to silently never register with the
            # organizer at all (guest shows "Connecting..." forever, nothing
            # ever appears in the admitter's lobby/People panel). Re-enable
            # only once that's ruled out or confirmed safe.
            log.info(f"Navigating to Teams Room Link: {meeting_url}")
            await page.goto(meeting_url, wait_until="domcontentloaded")

            await do_teams_join(page)

            # The audio payload script reads this to know which port the
            # voice bridge actually bound to (see the port-0 binding above).
            await page.evaluate(f"window.__VOICE_BRIDGE_PORT__ = {bound_port['value']};")
            await page.evaluate(custom_audio_js)
            log.info("Custom audio stream pipeline active. Waiting for meeting close...")

            # The page will now automatically close when the end_call_action clicks "Leave"
            await page.wait_for_event("close", timeout=0)

        except asyncio.CancelledError:
            log.info("Bot worker task received interrupt signal termination.")
        finally:
            stop_event.set()
            if context:
                await context.close()
            await server_task
            if guest_profile_dir:
                shutil.rmtree(guest_profile_dir, ignore_errors=True)


async def launch_admitter(meeting_url: str, ready_event: asyncio.Event | None = None) -> None:
    """Lightweight, no-audio session using the signed-in identity: joins the
    meeting (bypasses the lobby as an authenticated participant), watches for
    the anonymous guest bot to appear in the lobby, admits it, then leaves.
    Serialized via a Redis distributed lock (not asyncio.Lock) since the
    shared signed-in Chromium profile can now be targeted by workers running
    in separate OS processes — an in-process lock offers zero protection
    against two processes opening the same user-data-dir at once.
    """
    set_role("admitter")
    log = logging.getLogger("app.bot.admitter")

    try:
        async with held_lock(ADMITTER_IDENTITY_LOCK, ttl_ms=45000) as token:
            renew_task = asyncio.create_task(_renew_admitter_lock(token, log))
            try:
                await _run_admitter_session(meeting_url, ready_event, log)
            finally:
                renew_task.cancel()
    except TimeoutError:
        log.warning("Admitter identity is busy (locked by another interview); skipping this attempt.")


async def _renew_admitter_lock(token: str, log: logging.Logger) -> None:
    """Keeps the admitter's lock lease alive for as long as its session runs,
    in case a slow lobby/admit flow outlives the lock's initial TTL."""
    try:
        while True:
            await asyncio.sleep(15)
            renewed = await extend_lock(ADMITTER_IDENTITY_LOCK, token, ttl_ms=45000)
            if not renewed:
                log.warning("Admitter lock lease could not be renewed (lost ownership).")
                return
    except asyncio.CancelledError:
        pass


async def _run_admitter_session(meeting_url: str, ready_event: asyncio.Event | None, log: logging.Logger) -> None:
    log.info(f"Admitter: joining {meeting_url} to admit the guest bot.")
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(SIGNED_IN_PROFILE_DIR),
            headless=settings.browser_headless,
            **_browser_launch_channel_kwargs(),
            args=BROWSER_ARGS,
            permissions=["camera", "microphone"],
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            ignore_https_errors=True,
        )
        await _block_external_app_launch(context, log)

        page = context.pages[0] if context.pages else await context.new_page()
        page.on("console", lambda msg: log.info(f"[ADMITTER BROWSER] {msg.text}"))
        page.on("pageerror", lambda exc: log.error(f"[ADMITTER BROWSER ERROR] {exc}"))
        page.on("dialog", lambda dialog: asyncio.create_task(dialog.dismiss()))
        try:
            # Deliberately no interceptor.js / custom_audio_payload.js and no
            # connection to the realtime voice bridge here — the admitter's
            # only job is join -> admit -> confirm -> leave. It never touches
            # the OpenAI Realtime API or the interview audio pipeline.
            await Stealth().apply_stealth_async(page)
            await page.goto(meeting_url, wait_until="domcontentloaded")
            await do_teams_join(page)
            if ready_event is not None:
                ready_event.set()
                log.info("Admitter joined successfully; guest bot may now start.")
            admitted = await admit_from_lobby(page)
            if not admitted:
                log.warning("Admitter never saw a lobby prompt or couldn't confirm the guest joined.")
        except Exception as e:
            log.error(f"Admitter session failed: {e}")
        finally:
            left = await leave_teams_call(page, confirm=True)
            if not left:
                log.error("Admitter could not confirm it left the meeting.")
            await context.close()


async def launch_admitter_and_guest(
    meeting_url: str, details: dict, admitter_ready: asyncio.Event | None = None
) -> None:
    """Runs the admitter and guest as two tabs (BrowserContexts) inside a
    single shared browser process, instead of launch_admitter() and
    launch_bot(join_mode="guest") each launching their own separate browser.
    The admitter's tab opens first using its saved signed-in session
    (ADMITTER_STATE_FILE); once it has joined the meeting, a second tab opens
    for the anonymous guest, which the admitter then admits from the lobby.
    """
    log = logging.getLogger("app.bot.engine")
    ready_event = admitter_ready if admitter_ready is not None else asyncio.Event()

    try:
        async with held_lock(ADMITTER_IDENTITY_LOCK, ttl_ms=45000) as token:
            renew_task = asyncio.create_task(_renew_admitter_lock(token, log))
            try:
                async with async_playwright() as p:
                    browser = await p.chromium.launch(
                        headless=settings.browser_headless,
                        **_browser_launch_channel_kwargs(),
                        args=BROWSER_ARGS,
                    )
                    try:
                        await asyncio.gather(
                            _run_admitter_tab(browser, meeting_url, ready_event),
                            _run_guest_tab(browser, meeting_url, details, ready_event),
                        )
                    finally:
                        await browser.close()
            finally:
                renew_task.cancel()
    except TimeoutError:
        log.warning("Admitter identity is busy (locked by another interview); skipping this attempt.")


async def _run_admitter_tab(browser, meeting_url: str, ready_event: asyncio.Event) -> None:
    set_role("admitter")
    log = logging.getLogger("app.bot.admitter")
    log.info(f"Admitter: joining {meeting_url} to admit the guest bot.")

    context = await browser.new_context(storage_state=str(ADMITTER_STATE_FILE), **_CONTEXT_KWARGS)
    await _block_external_app_launch(context, log)
    page = await context.new_page()
    page.on("console", lambda msg: log.info(f"[ADMITTER BROWSER] {msg.text}"))
    page.on("pageerror", lambda exc: log.error(f"[ADMITTER BROWSER ERROR] {exc}"))
    page.on("dialog", lambda dialog: asyncio.create_task(dialog.dismiss()))
    try:
        # Deliberately no interceptor.js / custom_audio_payload.js and no
        # connection to the realtime voice bridge here — the admitter's only
        # job is join -> admit -> confirm -> leave.
        await Stealth().apply_stealth_async(page)
        await page.goto(meeting_url, wait_until="domcontentloaded")
        await do_teams_join(page)
        if not await start_recording(page):
            log.warning("Proceeding without confirmed recording.")
        ready_event.set()
        log.info("Admitter joined successfully; guest bot may now start.")
        admitted = await admit_from_lobby(page)
        if not admitted:
            log.warning("Admitter never saw a lobby prompt or couldn't confirm the guest joined.")
    except Exception as e:
        log.error(f"Admitter session failed: {e}")
    finally:
        left = await leave_teams_call(page, confirm=True)
        if not left:
            log.error("Admitter could not confirm it left the meeting.")
        await context.close()


async def _run_guest_tab(browser, meeting_url: str, details: dict, admitter_ready: asyncio.Event) -> None:
    set_role("guest")
    log = logging.getLogger("app.bot.guest")

    log.info("Guest bot waiting for admitter to join the meeting first.")
    try:
        # See launch_bot()'s matching wait for why this is 150s, not 60s.
        await asyncio.wait_for(admitter_ready.wait(), timeout=150)
        log.info("Admitter is ready; starting guest bot join.")
    except asyncio.TimeoutError:
        log.error("Admitter did not become ready within 150 seconds; guest bot will not join.")
        return

    stop_event = asyncio.Event()
    page_ref = {"page": None}

    async def end_call_action():
        if page_ref["page"]:
            await leave_teams_call(page_ref["page"])
        else:
            log.error("end_call_action triggered, but browser page is not active.")

    bound_port = {"value": None}

    async def start_server():
        async with websockets.serve(
            lambda ws: handle_voice_bridge_ws(ws, details, end_call_callback=end_call_action),
            settings.websocket_host,
            0,
        ) as server:
            bound_port["value"] = server.sockets[0].getsockname()[1]
            await stop_event.wait()

    server_task = asyncio.create_task(start_server())
    while bound_port["value"] is None:
        await asyncio.sleep(0.05)
    log.info(f"Voice bridge bound to port {bound_port['value']}")

    interceptor_js = _load_interceptor_js()
    with open(BASE_DIR / "scripts" / "custom_audio_payload.js", "r", encoding="utf-8") as f:
        custom_audio_js = f.read()
    with open(BASE_DIR / "scripts" / "stealth_injection.js", "r", encoding="utf-8") as f:
        stealth_js = f.read()

    context = await browser.new_context(**_CONTEXT_KWARGS)
    await _block_external_app_launch(context, log)

    page = context.pages[0] if context.pages else await context.new_page()
    page_ref["page"] = page

    page.on("console", lambda msg: log.info(f"[BROWSER] {msg.text}"))
    page.on("pageerror", lambda exc: log.error(f"[BROWSER ERROR] {exc}"))
    page.on("dialog", lambda dialog: asyncio.create_task(dialog.dismiss()))

    try:
        await Stealth().apply_stealth_async(page)
        await page.add_init_script(stealth_js)
        await page.add_init_script(interceptor_js)
        log.info(f"Navigating to Teams Room Link: {meeting_url}")
        await page.goto(meeting_url, wait_until="domcontentloaded")

        await do_teams_join(page)

        await page.evaluate(f"window.__VOICE_BRIDGE_PORT__ = {bound_port['value']};")
        await page.evaluate(custom_audio_js)
        log.info("Custom audio stream pipeline active. Waiting for meeting close...")

        await page.wait_for_event("close", timeout=0)

    except asyncio.CancelledError:
        log.info("Bot worker task received interrupt signal termination.")
    finally:
        stop_event.set()
        await context.close()
        await server_task