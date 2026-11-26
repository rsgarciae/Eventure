""".ics generation.

Zero credentials, works with any calendar app. This is the fallback that survives
every OAuth failure, so it never depends on the Google path working.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from .model import Event


def _escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold(line: str, limit: int = 75) -> str:
    """RFC 5545 folding, measured in UTF-8 octets, not characters."""
    parts: list[str] = []
    current = ""
    size = 0
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > limit:
            parts.append(current)
            current = " "
            size = 1
        current += char
        size += width
    parts.append(current)
    return "\r\n".join(parts)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%S")


def build(events: list[Event], tzname: str, calendar_name: str = "Eventure") -> str:
    now = datetime.now(tz=None).astimezone()
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Eventure//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(calendar_name)}",
        f"X-WR-TIMEZONE:{tzname}",
    ]
    for event in events:
        start = event.start if event.start.tzinfo else event.start.replace(tzinfo=now.tzinfo)
        end = event.end or (start + timedelta(hours=2))
        if end.tzinfo is None:
            end = end.replace(tzinfo=now.tzinfo)
        uid = event.uid or hashlib.sha1(event.title.encode("utf-8")).hexdigest()[:16]
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{uid}")
        lines.append(f"DTSTAMP:{_stamp(now.astimezone())}Z")
        if event.all_day:
            lines.append(f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}")
            lines.append(f"DTEND;VALUE=DATE:{(start + timedelta(days=1)).strftime('%Y%m%d')}")
        else:
            lines.append(f"DTSTART;TZID={tzname}:{_stamp(start)}")
            lines.append(f"DTEND;TZID={tzname}:{_stamp(end)}")
        lines.append(f"SUMMARY:{_escape(event.title)}")
        if event.location:
            lines.append(f"LOCATION:{_escape(event.location)}")
        description = event.url or f"Fuente: {event.source}"
        lines.append(f"DESCRIPTION:{_escape(description)}")
        if event.url:
            lines.append(f"URL:{event.url}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
