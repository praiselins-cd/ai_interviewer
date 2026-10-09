import asyncio
import contextvars
import logging
import re
import time
from pathlib import Path

from playwright.async_api import Page

_DEBUG_SNAPSHOT_DIR = Path(__file__).parent / "debug_snapshots"


async def _save_debug_snapshot(page: Page, label: str) -> None:
    """Saves a screenshot + the main frame's HTML to disk, so a timeout
    that means "the expected UI never appeared" (as opposed to "found it,
    clicked wrong") leaves visual evidence instead of just a vague log
    line -- there's no other way to see what the page actually looked like
    at the moment of failure, especially headless."""
    try:
        _DEBUG_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = f"{label}_{int(time.time())}"
        await page.screenshot(path=str(_DEBUG_SNAPSHOT_DIR / f"{stamp}.png"))
        html = await page.content()
        (_DEBUG_SNAPSHOT_DIR / f"{stamp}.html").write_text(html, encoding="utf-8")
        _log().warning(f"Saved debug snapshot: {stamp}.png / {stamp}.html")
    except Exception as e:  # noqa: BLE001 - a failed debug capture must never break the real flow
        _log().warning(f"Failed to save debug snapshot for {label}: {e}")

# Which browser session (guest bot vs signed-in admitter) is currently
# running, so every log line from this module — including the internal
# helpers below — is tagged with the right one even though both run
# concurrently as separate asyncio tasks. Each asyncio task gets its own
# copy of this context, so set_role() in one task never leaks into another.
_role_var = contextvars.ContextVar("bot_role", default="teams")


def set_role(role: str) -> None:
    _role_var.set(role)


def _log() -> logging.Logger:
    return logging.getLogger(f"app.bot.{_role_var.get()}")


async def _find_across_frames(page: Page, selector: str, timeout_ms: int = 0):
    """Search every frame (main frame + any iframes) for a visible match of
    `selector`, returning (frame, locator) for the first one found. Plain
    page.locator() only ever looks in the main frame, which silently finds
    nothing if the content (e.g. the People/lobby panel) actually lives
    inside a child iframe — a real gap Teams' web client hits often enough
    that every lobby lookup goes through this instead of page.locator
    directly. Polls every second up to timeout_ms if given; a single pass
    (default) if not.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000 if timeout_ms else None
    while True:
        for frame in page.frames:
            try:
                loc = frame.locator(selector).first
                if await loc.count() > 0 and await loc.is_visible():
                    return frame, loc
            except Exception:
                continue
        if deadline is None or loop.time() >= deadline:
            return None, None
        await asyncio.sleep(1)


# The guest bot's anonymous display name — used both to fill the join screen
# and, from the admitter's side, to find its specific row in the lobby panel
# (Teams can list several "Suspected threats" entries, so admitting by name
# is more reliable than admitting "whichever row is first").
GUEST_DISPLAY_NAME = "Proxy AI Interviewer"


async def _settle(page: Page, seconds: float = 1.5) -> None:
    """Pause after an interaction so the browser/Teams' React app has time to
    finish re-rendering before the next step touches the page. Best-effort
    waits for network activity to quiet down too, but never blocks long on it
    since Teams keeps background connections (websockets, polling) open
    indefinitely, which would make a strict networkidle wait hang.
    """
    try:
        await page.wait_for_load_state("networkidle", timeout=2000)
    except Exception:
        pass
    await asyncio.sleep(seconds)


async def _press_enter_or_click(page: Page, locator, action_name: str) -> None:
    """Prefer the same Enter-key action a user can perform, with a click fallback."""
    try:
        await locator.focus()
        await page.keyboard.press("Enter")
        _log().info(f"{action_name} submitted with the Enter key.")
    except Exception as key_error:
        _log().warning(f"{action_name} keyboard action failed; using click fallback: {key_error}")
        await locator.click(timeout=10000)


async def _dismiss_native_app_launch_prompt(page: Page) -> None:
    """Teams' join page tries to hand off to the native desktop app via a
    custom URL scheme, which triggers Chrome's own "Open Microsoft Teams?"
    bubble -- this is genuine browser-chrome UI (anchored below the
    omnibox), not page content and not a JS alert/confirm/prompt dialog, so
    it's invisible to page.locator() and to page.on("dialog") alike, and a
    network-route-based block (tried first, see engine.py's
    _block_external_app_launch) can't catch it either -- Chromium's
    ExternalProtocolHandler triggers it internally, before it ever becomes
    a request object the Network domain (and so context.route()) knows
    about. In headed mode a human just reflexively hits Escape/Cancel on
    it; Escape is the standard dismissal for this class of Chrome bubble,
    and it's dispatched through the same keyboard-input path Playwright
    already uses, so no OS-level automation dependency is needed."""
    for _ in range(2):
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)


async def _ensure_camera_on(page: Page) -> None:
    """Best-effort: turns the camera toggle on during the pre-join "green
    room" screen if it's found off. Nothing in this flow ever did this
    before -- the bot was joining with its camera left at whatever Teams
    defaults a fresh/incognito session to (commonly off, regardless of the
    camera permission already being granted), which is why the guest's
    video tile stayed black even once the fake canvas video track itself
    was confirmed working. Selectors here are unverified against a live
    Teams build (same category as the recording-menu selectors) -- never
    fatal, logs what it finds either way so a miss can be diagnosed from
    the logs instead of guessed at again.
    """
    try:
        candidates = [
            'button[aria-label*="camera" i]',
            '[data-tid="toggle-video"]',
            'button[data-tid*="camera" i]',
        ]
        cam_btn = None
        matched_selector = None
        for selector in candidates:
            loc = page.locator(selector).first
            if await loc.count() > 0:
                cam_btn = loc
                matched_selector = selector
                break
        if cam_btn is None:
            _log().warning(f"Camera toggle not found on pre-join screen (tried: {candidates}); leaving camera as-is.")
            return

        aria_label = await cam_btn.get_attribute("aria-label")
        aria_pressed = await cam_btn.get_attribute("aria-pressed")
        _log().info(
            f"Camera toggle found via '{matched_selector}': aria-label={aria_label!r}, aria-pressed={aria_pressed!r}"
        )

        # Teams labels this button with the action it would perform, so
        # aria-label containing "turn camera on" means it's currently OFF.
        # aria-pressed="false" is the same signal via a different attribute,
        # depending on the Teams build -- check both, since which one is
        # actually present is exactly what's unverified here.
        is_off = (aria_label and "on" in aria_label.lower()) or aria_pressed == "false"
        if is_off:
            await cam_btn.click(timeout=5000)
            await _settle(page)
            _log().info("Clicked the camera toggle to turn it on.")
        else:
            _log().info("Camera toggle appears to already be on; leaving it as-is.")
    except Exception as e:
        _log().warning(f"Could not confirm/enable the camera toggle: {e}")


async def do_teams_join(page: Page) -> None:
    name_input_selector = 'input[placeholder*="name" i], input[type="text"]'
    join_btn_selector = 'button:has-text("Join now")'

    await _dismiss_native_app_launch_prompt(page)

    # 1. Handle browser selection window
    _log().info("Teams UI: checking for the browser-selection prompt.")
    try:
        continue_btn = page.locator('button:has-text("Continue on this browser"), [data-tid="joinOnWeb"]')
        await continue_btn.wait_for(state="visible", timeout=15000)
        _log().info("Teams UI: browser-selection prompt found; submitting it.")
        # The native app-launch bubble's exact timing relative to page load
        # isn't guaranteed -- dismiss it again right here, immediately
        # before interacting with the button it can sit on top of.
        await _dismiss_native_app_launch_prompt(page)
        # Focus and press Enter first, matching the keyboard path used by a user.
        await asyncio.sleep(3)
        await _press_enter_or_click(page, continue_btn, "Continue on this browser")

        # _press_enter_or_click's Enter-key path never actually confirms the
        # page reacted -- focus()/keyboard.press() succeed regardless of
        # whether the underlying React handler fired, so this has been
        # observed to silently no-op, leaving the interstitial on screen
        # with nothing downstream ever finding out. Verify it actually
        # went away; if not, force a real click (bypassing the Enter path
        # entirely) and verify again.
        try:
            await continue_btn.wait_for(state="hidden", timeout=5000)
            _log().info("Successfully skipped OS protocol launcher prompt.")
        except Exception:
            _log().warning(
                "Continue-on-this-browser click had no visible effect after the Enter-key "
                "attempt; forcing a direct click."
            )
            await continue_btn.click(timeout=10000)
            await continue_btn.wait_for(state="hidden", timeout=8000)
            _log().info("Successfully skipped OS protocol launcher prompt (via click fallback).")
        await _settle(page, 8)
    except Exception as e:
        _log().info(f"Teams UI: browser-selection prompt not present: {e}")

    # Wait only after the browser-selection prompt is gone. Anonymous joins
    # expose a name field; authenticated joins normally expose Join now directly.
    _log().info("Teams UI: waiting for name field or Join now button.")
    try:
        await page.locator(f'{name_input_selector}, {join_btn_selector}').first.wait_for(
            state="visible", timeout=20000
        )
        _log().info("Teams UI: pre-join controls are visible.")
    except Exception as e:
        _log().warning(f"Teams UI: pre-join controls were not detected: {e}")
        await _save_debug_snapshot(page, "pre_join_controls_not_detected")

    # 2. Complete identity validation field (only shown for anonymous/unauthenticated joins;
    # if the persistent profile is already signed in to a Microsoft account, Teams skips
    # straight to the join screen and this field never appears)
    try:
        name_input = page.locator(name_input_selector).first
        await name_input.wait_for(state="visible", timeout=8000)
        _log().info(f"Teams UI: name field found; entering {GUEST_DISPLAY_NAME}.")
        # fill() auto-waits for the input to be enabled/editable before typing,
        # so it won't silently no-op on a field that's rendered but not yet live.
        await asyncio.sleep(1)
        await name_input.fill("")
        await name_input.type(GUEST_DISPLAY_NAME, delay=50)
        # Confirm the value actually stuck (catches cases where the field got
        # re-rendered mid-type and swallowed the input).
        if await name_input.input_value() != GUEST_DISPLAY_NAME:
            await name_input.fill(GUEST_DISPLAY_NAME)

        await page.keyboard.press("Tab")
        await _settle(page, 2)
        _log().info("Teams UI: display name entered successfully.")
    except Exception as e:
        _log().info(f"Teams UI: no anonymous name field was available; using signed-in join flow: {e}")

    await _ensure_camera_on(page)

    # 3. Enter lobby queue, with a confirmed retry in case the first click
    # landed before the button was truly interactive.
    join_dispatched = False
    for attempt in (1, 2):
        try:
            join_btn = page.locator(join_btn_selector).first
            await join_btn.wait_for(state="visible", timeout=15000)
            await _press_enter_or_click(page, join_btn, "Join now")
            await _settle(page, 2)
            _log().info(f"Dispatched lobby admittance request (attempt {attempt}).")
            join_dispatched = True
            break
        except Exception as e:
            _log().warning(f"Join now click attempt {attempt} failed: {e}")
            await asyncio.sleep(2)

    if not join_dispatched:
        _log().error("Crucial entry button element interaction error: Join now never became clickable.")
        await _save_debug_snapshot(page, "join_now_never_clickable")

    # 4. Await verification gate
    in_meeting_selectors = [
        'button[aria-label*="Leave" i]', 
        'button[data-tid="hangup-button"]',
        '#hangup-button',
        'button[aria-label*="Hang up" i]'
    ]
    
    _log().info("Awaiting lobby clearance from meeting organizer...")
    try:
        await page.wait_for_selector(", ".join(in_meeting_selectors), timeout=60000)
        _log().info("Meeting connection confirmed. Participant interface loaded.")
    except Exception:
        _log().warning("Lobby timeout exceeded; executing fallback setup injection processing.")
        await page.wait_for_timeout(5000)


async def wait_for_guest_to_actually_join(
    page: Page, display_name: str = GUEST_DISPLAY_NAME, timeout_s: int = 15, poll_interval_s: float = 1.0
) -> bool:
    """Confirms the guest is actually in the meeting (not just admitted from
    the lobby) before the admitter leaves, using the People panel that
    admit_from_lobby already has open: polls until the "Waiting in lobby"
    heading is gone (nobody left waiting) while the guest's name is still
    present somewhere on the page (they exist, as opposed to the list just
    emptying because they disconnected). Reuses _find_across_frames like
    every other check in this file -- no new selectors.

    Previously used a roster-badge aria-label regex count instead, which
    silently returned "unknown" for the whole 40s timeout whenever the real
    aria-label text didn't match the expected "(N)" pattern, leaving the
    admitter sitting in the meeting for ~43s after the guest had already
    visibly joined. This check only needs what's already proven to render:
    the same "Waiting in lobby" text admit_from_lobby just searched for.
    """
    _log().info("Confirming guest has actually joined before admitter leaves...")
    waiting_heading_selector = 'text=Waiting in lobby'
    name_selector = f'text={display_name}'
    attempts = max(1, int(timeout_s / poll_interval_s))
    for i in range(attempts):
        _, waiting_heading = await _find_across_frames(page, waiting_heading_selector, timeout_ms=0)
        _, name_match = await _find_across_frames(page, name_selector, timeout_ms=0)
        if waiting_heading is None and name_match is not None:
            _log().info(f"Confirmed guest is in the meeting (not waiting) after {i * poll_interval_s:.1f}s.")
            return True
        if i % 5 == 0:
            _log().info(f"Still waiting for guest to join (check #{i}, elapsed {i * poll_interval_s:.1f}s)...")
        await asyncio.sleep(poll_interval_s)

    _log().warning("Could not confirm guest joined via the People panel; using short fallback buffer wait.")
    await asyncio.sleep(3)
    return False


async def _debug_dump_name_matches(page: Page) -> None:
    """Best-effort diagnostic for when the guest's row genuinely can't be
    found: reports, per frame, how many elements contain the name at all
    (ignoring visibility) and dumps a snippet of the first match's outer
    HTML, so the next failure gives real signal instead of another guess.
    """
    try:
        for frame in page.frames:
            try:
                loc = frame.locator(f'text={GUEST_DISPLAY_NAME}')
                count = await loc.count()
                if count == 0:
                    continue
                first = loc.first
                visible = await first.is_visible()
                html = await first.evaluate("el => el.outerHTML && el.outerHTML.slice(0, 500)")
                _log().warning(
                    f"[debug] frame={frame.url} matches={count} first_visible={visible} "
                    f"first_outerHTML={html!r}"
                )
            except Exception as fe:
                _log().warning(f"[debug] frame={frame.url} inspection failed: {fe}")
        _log().warning(f"[debug] page.frames total={len(page.frames)}")
    except Exception as e:
        _log().warning(f"[debug] name-match dump failed entirely: {e}")


_DIALOG_HEADING_SELECTOR = 'text="Admit this unverified bot?"'


async def _confirm_unverified_bot_dialog_if_present(page: Page, wait_ms: int = 8000) -> bool:
    """Any admit action (row click, context-menu click, whatever) can pop a
    second confirmation modal ("Admit this unverified bot?") with its own
    separate exact-text "Admit" control. This must run after EVERY admit
    attempt, not just the right-click flow — a direct row click was found to
    trigger this same modal too, and that code path originally didn't know
    to expect it, so the modal was left sitting there unclicked. Returns
    True if no dialog appeared (nothing further needed) or it appeared and
    was successfully dismissed; False if it appeared but couldn't be.
    """
    _, dialog_heading = await _find_across_frames(page, _DIALOG_HEADING_SELECTOR, timeout_ms=wait_ms)
    if dialog_heading is None:
        return True

    _log().info("Confirmation dialog ('Admit this unverified bot?') appeared; clicking its Admit control.")
    # Not restricted to a <button> tag (may be a styled div/role element),
    # and not via Enter (its default target here isn't guaranteed to be
    # Admit rather than Deny).
    _, dialog_admit = await _find_across_frames(page, 'text="Admit"', timeout_ms=5000)
    if dialog_admit is None:
        _log().warning("Confirmation dialog appeared but its Admit control could not be located.")
        return False
    try:
        await dialog_admit.click(timeout=5000)
        await _settle(page)
        _log().info("Clicked Admit in the confirmation dialog.")
    except Exception as e:
        _log().warning(f"Confirmation dialog Admit control could not be clicked: {e}")
        return False

    # Confirm the dialog actually closed, i.e. the click really landed.
    _, still_open = await _find_across_frames(page, _DIALOG_HEADING_SELECTOR, timeout_ms=8000)
    if still_open is not None:
        _log().warning("Clicked Admit but the confirmation dialog is still visible; the click may not have registered.")
        return False
    return True


async def _admit_via_context_menu(page: Page, row, display_name: str = GUEST_DISPLAY_NAME) -> bool:
    """Tenants that flag an anonymous/external joiner as a "Suspected threat"
    (People panel -> Waiting in lobby -> Suspected threats) don't show a
    plain Admit button on that row. The waiting participant's row (already
    located by the caller, admit_from_lobby) has to be right-clicked to open
    a context menu with its own Admit action, and confirming that can pop a
    second dialog with yet another Admit button to click. `display_name` is
    only used for logging here — admits by name, so it's admitting the
    guest bot (default) or, generalized for Module 2, the real candidate.
    """
    # The context menu item isn't guaranteed to carry a proper ARIA role
    # (role="menuitem"), so it's detected by its actual visible text instead:
    # "Admit participant in lobby" (confirmed from a live screenshot).
    menu_item_selector = 'text="Admit participant in lobby"'
    try:
        await row.click(button="right", timeout=5000)
        _log().info(f"Right-clicked {display_name}'s lobby row.")
        await _settle(page)

        # Right-clicking might open the confirmation modal directly with no
        # intermediate menu at all, depending on the Teams build — so both
        # possibilities are watched for.
        _, combined = await _find_across_frames(
            page, f"{menu_item_selector}, {_DIALOG_HEADING_SELECTOR}", timeout_ms=8000
        )
        if combined is None:
            _log().warning("Neither a context menu nor the confirmation dialog appeared after right-click.")
            return False

        _, menu_item = await _find_across_frames(page, menu_item_selector, timeout_ms=0)
        if menu_item is not None:
            _log().info("Context menu appeared; clicking its 'Admit participant in lobby' entry.")
            # Direct click, not the Enter-key path: a text= match often
            # resolves to a non-focusable text node, where focus() silently
            # no-ops and nothing happens, yet no exception is raised.
            await menu_item.click(timeout=5000)
            await _settle(page)
        else:
            _log().info("Confirmation dialog appeared directly (no separate context menu).")

        return await _confirm_unverified_bot_dialog_if_present(page)
    except Exception as e:
        _log().info(f"Right-click Admit flow did not apply or failed: {e}")
        return False


async def start_recording(page: Page) -> bool:
    """Best-effort: opens Teams' "More actions" menu and starts cloud
    recording. Teams records at the meeting level, so it keeps recording
    after the admitter later leaves -- this only needs to be clicked once,
    by the admitter, before admitting the guest. Selectors here are
    unverified against a live Teams build (unlike the join/admit flow
    above); any failure is logged and swallowed -- never fatal to the
    interview, since the caller treats recording as optional.
    """
    try:
        # Tried in order: a visible-text match on the toolbar's "More" label
        # (confirmed from a live screenshot) first, then looser aria-label/id
        # fallbacks in case the text match misses on a different Teams build.
        more_btn = None
        for selector in (
            'button:text-is("More")',
            'button[aria-label="More" i]',
            'button[aria-label*="More" i]',
            'button[id*="more-btn" i]',
        ):
            _, more_btn = await _find_across_frames(page, selector, timeout_ms=5000)
            if more_btn is not None:
                _log().info(f"Found the More actions button via selector: {selector}")
                break
        if more_btn is None:
            _log().warning("Could not find the More actions button; skipping recording.")
            return False
        await more_btn.click(timeout=5000)
        await _settle(page)

        # Current Teams groups this under a "Record and transcribe" flyout
        # submenu, itself containing "Start recording" / "Start transcription".
        _, submenu = await _find_across_frames(page, 'text="Record and transcribe"', timeout_ms=5000)
        if submenu is not None:
            await submenu.click(timeout=5000)
            await _settle(page)

        _, start_item = await _find_across_frames(page, 'text="Start recording"', timeout_ms=5000)
        if start_item is None:
            _log().warning("Could not find 'Start recording' menu item; skipping recording.")
            return False
        await start_item.click(timeout=5000)
        await _settle(page)

        # Clicking "Start recording" opens a "Start recording and
        # transcription" dialog (language picker + "Choose what to record",
        # already defaulted to "Video and audio") with a "Confirm" button.
        _, confirm_btn = await _find_across_frames(page, 'button:has-text("Confirm")', timeout_ms=5000)
        if confirm_btn is not None:
            await confirm_btn.click(timeout=5000)
            await _settle(page)
        else:
            _log().warning("Start-recording confirmation dialog's Confirm button was not found.")

        _log().info("Recording start sequence completed.")
        return True
    except Exception as e:
        _log().warning(f"Could not start recording: {e}")
        await _save_debug_snapshot(page, "start_recording_failed")
        return False


async def admit_from_lobby(page: Page, timeout_ms: int = 120000, display_name: str = GUEST_DISPLAY_NAME) -> bool:
    """Opens the People panel, finds a waiting participant's row under
    Waiting in lobby by their display name, and admits them. `display_name`
    defaults to the guest bot's own name (its original purpose — the
    signed-in admitter letting the anonymous guest bot in), but is a real
    parameter so the same function also admits the real candidate later in
    the flow (Module 2's WAITING_FOR_CANDIDATE state) without duplicating
    any of this logic.
    Returns True if an admit action was taken and confirmed, False otherwise.

    NOTE: deliberately does NOT do an unscoped page-wide search for
    'button:has-text("Admit")' as a "direct toast" shortcut — that matched a
    false positive somewhere else in Teams' DOM in practice (some hidden/
    unrelated element containing the substring "Admit"), got clicked, and
    silently did nothing while the guest sat in the lobby. Every admit
    attempt below is scoped to the named row.
    """
    # The lobby entries live in the People panel (Waiting in lobby ->
    # Suspected threats), opened via the People icon in the meeting toolbar.
    people_panel_selectors = [
        'button:has-text("People")',
        '[aria-label*="People" i]',
        '[data-tid="roster-button"]',
    ]
    waiting_heading_selector = 'text=Waiting in lobby'
    # Substring match, not exact — see the matching note above;
    # exact text="..." failed to match even across every frame despite the
    # name being visibly on screen.
    name_substring_selector = f'text={display_name}'

    _log().info("Admitter is in the meeting; opening the People panel to check the lobby.")
    try:
        people_btn = page.locator(", ".join(people_panel_selectors)).first
        await people_btn.wait_for(state="visible", timeout=15000)
        await _press_enter_or_click(page, people_btn, "People")
        await _settle(page)
        _log().info("People panel opened.")
    except Exception as e:
        _log().warning(f"Could not open the People panel: {e}")
        return False

    # Confirm the "Waiting in lobby" section itself rendered before hunting
    # for the guest's name — if it never appears, either nobody's actually
    # waiting yet or the panel didn't render the way we expect, and there's
    # no point searching the whole page for the name in that case.
    waiting_frame, waiting_heading = await _find_across_frames(page, waiting_heading_selector, timeout_ms=timeout_ms)
    if waiting_heading is None:
        _log().warning(f"'Waiting in lobby' section never appeared in any frame within {timeout_ms}ms.")
        await _debug_dump_name_matches(page)
        return False
    _log().info(f"'Waiting in lobby' section confirmed (frame: {waiting_frame.url}).")

    # Scope the name search to elements that come after the "Waiting in
    # lobby" heading in document order, within the same frame it was found
    # in — i.e. actually under that section, not just anywhere on the page.
    scoped_name_xpath = (
        'xpath=//*[contains(normalize-space(string(.)), "Waiting in lobby")]'
        f'/following::*[contains(normalize-space(string(.)), "{display_name}")]'
    )
    row = waiting_frame.locator(scoped_name_xpath).first
    try:
        await row.wait_for(state="visible", timeout=15000)
        _log().info(f"Found {display_name}'s row under Waiting in lobby.")
    except Exception as e:
        _log().warning(f"{display_name} not found under Waiting in lobby (scoped search): {e}")
        # Fall back to an unscoped cross-frame search in case the scoping
        # xpath itself doesn't match Teams' actual DOM shape.
        row_frame, row = await _find_across_frames(page, name_substring_selector, timeout_ms=15000)
        if row is None:
            _log().warning(f"{display_name}'s row never appeared anywhere either.")
            await _debug_dump_name_matches(page)
            return False
        _log().info(f"Found {display_name}'s row via unscoped fallback (frame: {row_frame.url}).")

    # Some tenants show a plain Admit control right on the row (no
    # "suspected"/"unverified" flag); scoped to the row itself so it can't
    # match anything unrelated elsewhere on the page.
    row_admit = row.locator('button:has-text("Admit"), [aria-label*="Admit" i]').first
    try:
        await row_admit.wait_for(state="visible", timeout=5000)
        await _press_enter_or_click(page, row_admit, "Admit (row)")
        await _settle(page)
        _log().info("Clicked a plain Admit control directly on the lobby row.")
        # This click was found to trigger the same "Admit this unverified
        # bot?" confirmation modal as the right-click flow — must check for
        # and dismiss it here too, not just declare success immediately.
        if await _confirm_unverified_bot_dialog_if_present(page):
            await wait_for_guest_to_actually_join(page)
            return True
        _log().warning("Row Admit control triggered a confirmation dialog that could not be dismissed.")
        return False
    except Exception:
        _log().info("No plain Admit control on the row; trying the right-click context menu flow.")

    if await _admit_via_context_menu(page, row, display_name=display_name):
        await wait_for_guest_to_actually_join(page)
        return True

    _log().warning("Could not admit the guest via the row's Admit control or the right-click flow.")
    return False


async def leave_teams_call(page: Page, confirm: bool = False) -> bool:
    """Clicks the hangup/leave button to end the call.
    If confirm=True, also waits for the in-meeting controls to disappear so
    the caller knows the departure actually completed, not just that the
    click was dispatched. Returns True if departure was confirmed (or
    confirm=False and the click succeeded), False otherwise.
    """
    leave_selectors = [
        'button[aria-label*="Leave" i]',
        'button[data-tid="hangup-button"]',
        '#hangup-button',
        'button[aria-label*="Hang up" i]'
    ]
    joined_selector = ", ".join(leave_selectors)
    _log().info("Attempting to leave the Teams meeting...")
    try:
        btn = page.locator(joined_selector).first
        await btn.click(timeout=5000)
        await _settle(page)
        _log().info("Clicked the leave/hangup button.")
    except Exception as e:
        _log().warning(f"Could not cleanly click leave button (might already be disconnected): {e}")
        return False

    if not confirm:
        return True

    try:
        await page.wait_for_selector(joined_selector, state="hidden", timeout=15000)
        _log().info("Confirmed departure: in-meeting controls are gone.")
        return True
    except Exception as e:
        _log().warning(f"Could not confirm departure from the meeting: {e}")
        return False