"""
One-time interactive setup: signs the bot's dedicated Chrome profile into
Microsoft Teams so that future automated joins (engine.py) are authenticated
instead of anonymous, which lets the org's normal lobby-bypass policy admit
the bot automatically instead of leaving it stuck in the lobby.

Run this manually once (not part of the API flow):
    python -m app.bot.login_setup

A real Chrome window will open to teams.microsoft.com. Sign in by hand
(email, password, MFA) with the bot account, wait until you see the Teams
app home screen, then come back to this terminal and press Enter (the
session must be saved before the browser window closes, since closing it
ends the connection this script needs to read cookies/localStorage from).
The session is written to app/bot/admitter_state.json (a storage_state
snapshot, not a profile directory) and will be loaded automatically by
engine.py's admitter context.
"""
import asyncio
import os
from pathlib import Path
from playwright.async_api import async_playwright

from app.config import get_settings

BASE_DIR = Path(__file__).parent
PROFILE_DIR = Path(os.environ.get("LOGIN_PROFILE_DIR", str(BASE_DIR / "browser_profile")))
STATE_FILE = BASE_DIR / "admitter_state.json"

settings = get_settings()


async def main():
    launch_kwargs = (
        {"executable_path": settings.browser_executable_path}
        if settings.browser_executable_path
        else {"channel": "chrome"}
    )
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            **launch_kwargs,
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://teams.microsoft.com")
        print("Sign in with the bot's Microsoft account in the opened window.")
        print("IMPORTANT: do NOT close the browser window yourself.")
        print("Once you see the Teams home screen, come back here and press Enter.")
        await asyncio.to_thread(input)

        if page.is_closed():
            print(
                "ERROR: the browser window was closed before Enter was pressed, "
                "so the session could not be saved. Please re-run this script, "
                "sign in again, and press Enter WITHOUT closing the browser window."
            )
            return

        await context.storage_state(path=str(STATE_FILE))
        print(f"Saved session to {STATE_FILE}")
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
