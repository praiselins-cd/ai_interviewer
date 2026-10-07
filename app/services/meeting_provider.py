"""MeetingProvider abstraction so Teams/Graph can be swapped for another
provider later without touching the scheduling service. Only one concrete
implementation exists today (TeamsMeetingProvider, Graph-backed) — kept in
this same file since it's small, per the simplified plan.
"""
import asyncio
from abc import ABC, abstractmethod

from app.integrations import graph_client


class MeetingProvider(ABC):
    @abstractmethod
    async def create_meeting(self, data: dict) -> dict:
        """data: {subject, start_iso, end_iso}. Returns at least
        {"meeting_id": str, "join_web_url": str}."""
        raise NotImplementedError

    @abstractmethod
    async def update_meeting(self, meeting_id: str, data: dict) -> dict:
        raise NotImplementedError

    @abstractmethod
    async def cancel_meeting(self, meeting_id: str) -> None:
        raise NotImplementedError


class TeamsMeetingProvider(MeetingProvider):
    """Meeting CRUD is Graph-backed (see app/integrations/graph_client.py),
    NOT Playwright — Playwright stays scoped to actually *joining*/running
    the interview (app/bot/engine.py), which the worker orchestrates
    separately from this provider.
    """

    async def create_meeting(self, data: dict) -> dict:
        # graph_client uses the blocking `requests` library — run it off the
        # event loop thread so it doesn't stall every other coroutine in the
        # process (the FastAPI server, the scheduler loop, etc.) for the
        # duration of the HTTP call.
        result = await asyncio.to_thread(
            graph_client.create_online_meeting, data["subject"], data["start_iso"], data["end_iso"]
        )
        return {"meeting_id": result["id"], "join_web_url": result["joinWebUrl"]}

    async def update_meeting(self, meeting_id: str, data: dict) -> dict:
        return await asyncio.to_thread(graph_client.update_online_meeting, meeting_id, data)

    async def cancel_meeting(self, meeting_id: str) -> None:
        await asyncio.to_thread(graph_client.cancel_online_meeting, meeting_id)
