"""
One-time interactive setup: signs the bot's dedicated Chrome profile into
Microsoft Teams so that future automated joins (engine.py) are authenticated
instead of anonymous, which lets the org's normal lobby-bypass policy admit
the bot automatically instead of leaving it stuck in the lobby.

Run this manually once (not part of the API flow):
    python -m app.bot.login_setup

A real Chrome window will open to teams.microsoft.com. Sign in by hand
(email, password, MFA) with the bot account, wait until you see the Teams
app home screen, then close the browser window. The session is written to
app/bot/browser_profile and will be reused automatically by engine.py.
"""
import asyncio
import os
from pathlib import Path
from playwright.async_api import async_playwright

from app.config import get_settings

BASE_DIR = Path(__file__).parent
PROFILE_DIR = Path(os.environ.get("LOGIN_PROFILE_DIR", str(BASE_DIR / "browser_profile")))

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
        print("Once you see the Teams home screen, close this browser window to finish.")
        await page.wait_for_event("close", timeout=0)
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
