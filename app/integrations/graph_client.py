"""Minimal Microsoft Graph client for creating/updating/cancelling Teams
meetings via the signed-in bot account, using the app registration that
already existed in .env (TEAMS_CLIENT_ID/TENANT_ID/CLIENT_SECRET) but was
previously unused anywhere in this codebase.
"""
import logging
import time

import msal
import requests

from app.config import get_settings

logger = logging.getLogger("app.integrations.graph")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]

_token_cache: dict = {"access_token": None, "expires_at": 0}


def _get_access_token() -> str:
    """Client-credentials flow, cached until shortly before expiry. This app
    registration must be granted the OnlineMeetings.ReadWrite.All (or
    equivalent) application permission with admin consent for this to work —
    that's a tenant-admin action outside this codebase's control."""
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    settings = get_settings()
    app = msal.ConfidentialClientApplication(
        client_id=settings.teams_client_id,
        client_credential=settings.teams_client_secret,
        authority=f"https://login.microsoftonline.com/{settings.teams_tenant_id}",
    )
    result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)
    if "access_token" not in result:
        raise RuntimeError(
            f"Failed to acquire Graph token: {result.get('error')}: {result.get('error_description')}"
        )

    _token_cache["access_token"] = result["access_token"]
    _token_cache["expires_at"] = now + result.get("expires_in", 3600)
    return _token_cache["access_token"]


def _headers() -> dict:
    return {"Authorization": f"Bearer {_get_access_token()}", "Content-Type": "application/json"}


def _raise_with_body(resp: requests.Response) -> None:
    """`resp.raise_for_status()` alone discards the response body, which is
    where Graph actually puts the useful error (e.g. missing admin consent,
    licensing issue, invalid organizer) -- log it before raising so it's not
    lost."""
    if resp.status_code >= 400:
        logger.error(f"Graph API error {resp.status_code} for {resp.url}: {resp.text}")
    resp.raise_for_status()


def _organizer_id(organizer_override: str | None = None) -> str:
    """The onlineMeetings create/update/delete endpoints require the
    organizer's Azure AD object ID (GUID) under application permissions --
    the email/UPN is rejected (see _raise_with_body's docstring context and
    teams_user_object_id in config.py). An explicit override is passed
    through as-is (assumed to already be a GUID from the caller)."""
    if organizer_override:
        return organizer_override
    settings = get_settings()
    return settings.teams_user_object_id or settings.teams_user_email


def create_online_meeting(subject: str, start_iso: str, end_iso: str, organizer_email: str | None = None) -> dict:
    """Creates a Teams meeting via Graph, with the lobby set to bypass for
    everyone — this is what lets both the admitter and the guest bot (and
    the real candidate) get straight into the call instead of ever queuing
    in the lobby, for meetings this system creates itself."""
    organizer = _organizer_id(organizer_email)
    resp = requests.post(
        f"{GRAPH_BASE}/users/{organizer}/onlineMeetings",
        headers=_headers(),
        json={
            "subject": subject,
            "startDateTime": start_iso,
            "endDateTime": end_iso,
            "lobbyBypassSettings": {"scope": "everyone"},
        },
        timeout=15,
    )
    _raise_with_body(resp)
    data = resp.json()
    logger.info(f"Created Graph online meeting {data.get('id')}: {data.get('joinWebUrl')}")
    return data


def update_online_meeting(meeting_id: str, updates: dict, organizer_email: str | None = None) -> dict:
    organizer = _organizer_id(organizer_email)
    resp = requests.patch(
        f"{GRAPH_BASE}/users/{organizer}/onlineMeetings/{meeting_id}",
        headers=_headers(),
        json=updates,
        timeout=15,
    )
    _raise_with_body(resp)
    return resp.json() if resp.content else {}


def cancel_online_meeting(meeting_id: str, organizer_email: str | None = None) -> None:
    """Graph doesn't offer a real 'cancel' for onlineMeetings created this
    way (they're not calendar events) — deleting it is the closest
    equivalent, which stops it from being joinable."""
    organizer = _organizer_id(organizer_email)
    resp = requests.delete(
        f"{GRAPH_BASE}/users/{organizer}/onlineMeetings/{meeting_id}",
        headers=_headers(),
        timeout=15,
    )
    _raise_with_body(resp)
