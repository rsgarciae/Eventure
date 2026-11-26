"""Google Calendar writer: raw REST over stdlib, no client library.

Scope used: https://www.googleapis.com/auth/calendar.events
That is enough to insert events and nothing else.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from .http import urlopen

TOKEN_URI = "https://oauth2.googleapis.com/token"
EVENTS_URI = "https://www.googleapis.com/calendar/v3/calendars/{calendar}/events"


class GoogleError(RuntimeError):
    pass


def load_token(path: Path) -> dict:
    if path.is_dir():
        raise GoogleError(
            f"{path} is a directory — Docker bind-mounts a missing file as an empty "
            "directory. Create token.json first, or remove [google].token_path"
        )
    if not path.exists():
        raise GoogleError(f"{path} not found — run google_auth.py on a machine with a browser")
    token = json.loads(path.read_text(encoding="utf-8"))
    if not token.get("refresh_token"):
        raise GoogleError(f"{path} has no refresh_token — re-run google_auth.py")
    return token


def _post_form(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise GoogleError(f"token exchange failed: HTTP {exc.code} {detail}") from exc


def access_token(token: dict) -> str:
    """Exchange the stored refresh token for a short-lived access token."""
    refreshed = _post_form(
        token.get("token_uri", TOKEN_URI),
        {
            "client_id": token["client_id"],
            "client_secret": token["client_secret"],
            "refresh_token": token["refresh_token"],
            "grant_type": "refresh_token",
        },
    )
    access = refreshed.get("access_token")
    if not access:
        raise GoogleError(f"no access_token in response: {refreshed}")
    return access


def _body(event, tzname: str) -> dict:
    start = event.start
    if event.all_day:
        start_block = {"date": start.date().isoformat()}
        end_block = {"date": (start.date() + timedelta(days=1)).isoformat()}
    else:
        local = start if start.tzinfo else start.replace(tzinfo=None)
        end = event.end or (start + timedelta(hours=2))
        start_block = {"dateTime": local.isoformat(timespec="seconds"), "timeZone": tzname}
        end_block = {"dateTime": end.isoformat(timespec="seconds"), "timeZone": tzname}
    body = {
        "summary": event.title,
        "start": start_block,
        "end": end_block,
    }
    if event.location:
        body["location"] = event.location
    description = event.url or ""
    if event.source:
        description = (description + f"\n\nvía {event.source}").strip()
    if description:
        body["description"] = description
    return body


def insert_event(token: dict, calendar: str, event, tzname: str) -> str:
    """Create the event, refresh once on 401, return the Google event id."""
    payload = json.dumps(_body(event, tzname)).encode("utf-8")
    url = EVENTS_URI.format(calendar=urllib.parse.quote(calendar, safe=""))
    for attempt in (1, 2):
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {access_token(token)}",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                created = json.loads(response.read().decode("utf-8"))
            return str(created.get("id", ""))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            if exc.code == 401 and attempt == 1:
                continue  # stale access token: refresh and retry once
            raise GoogleError(f"insert failed: HTTP {exc.code} {detail}") from exc
    raise GoogleError("insert failed after refresh")
