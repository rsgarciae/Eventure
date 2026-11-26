#!/usr/bin/env python3
"""Phase 0 feed audit for Eventure.

Answers exactly one question: for each source I already watch manually, can I get
upcoming events *without* scraping or credentials?

    python3 audit.py sources.txt
    python3 audit.py sources.txt --probe --json audit.json
    python3 audit.py --selftest

Stdlib only. No credentials, no database, no writes outside the optional --json
report.

Verdict per source:
    WORKS  machine-readable and yields usable content
    EMPTY  machine-readable but nothing upcoming / nothing recent
    NONE   no machine-readable feed found
    ERROR  fetch failed
"""

from __future__ import annotations

import argparse
import json as jsonlib
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit
from urllib.request import urlopen as _urlopen

try:  # share the CA-bundle resolution when running from the repo
    from eventure.http import ssl_context as _ssl_context
except ImportError:  # pragma: no cover - keeps this file usable standalone

    def _ssl_context():
        import ssl

        return ssl.create_default_context()
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

USER_AGENT = "eventure-audit/0.1 (personal feed audit)"
FEED_TYPES = (
    "text/calendar",
    "application/rss+xml",
    "application/atom+xml",
)
PROBE_PATHS = ("/feed", "/rss", "/rss.xml", "/atom.xml", "/events.ics", "/calendar.ics")
MAX_BYTES = 4_000_000

# Hosts where a third-party account structurally has no readable feed. These are
# not "missing feeds" — they are Phase 3 territory. Counting them as feed failures
# would silently drag the verdict towards "scraping required" for the wrong reason.
SOCIAL_HOSTS = (
    "instagram.com",
    "facebook.com",
    "fb.com",
    "fb.me",
    "threads.net",
    "tiktok.com",
    "x.com",
    "twitter.com",
)


def social_platform(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower()
    for domain in SOCIAL_HOSTS:
        if host == domain or host.endswith("." + domain):
            return domain.split(".")[0]
    return None


# --------------------------------------------------------------------------- model


@dataclass
class Event:
    title: str
    start: datetime | None = None
    uid: str = ""
    location: str = ""
    url: str = ""
    image: str = ""


@dataclass
class Result:
    name: str
    url: str
    kind: str | None = None  # "ics" | "rss" | None
    status: str = "NONE"
    items: int = 0
    upcoming: int = 0
    newest: datetime | None = None
    samples: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------- fetching


def fetch(url: str, timeout: float):
    """Return (status, content_type, body, error)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with _urlopen(request, timeout=timeout, context=_ssl_context()) as response:
            body = response.read(MAX_BYTES)
            return response.status, response.headers.get("Content-Type", ""), body, None
    except urllib.error.HTTPError as exc:
        return exc.code, (exc.headers.get("Content-Type", "") if exc.headers else ""), b"", f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - audit tool, report and continue
        return None, "", b"", f"{type(exc).__name__}: {exc}"


def decode(body: bytes, content_type: str) -> str:
    charset = "utf-8"
    match = re.search(r"charset=([\w-]+)", content_type, re.I)
    if match:
        charset = match.group(1)
    return body.decode(charset, errors="replace")


# ---------------------------------------------------------------------- detection


def classify(content_type: str, body: bytes) -> str | None:
    head = body[:2048].lstrip()
    if "text/calendar" in content_type.lower() or head.startswith(b"BEGIN:VCALENDAR"):
        return "ics"
    if head.startswith(b"BEGIN:VCALENDAR"):
        return "ics"
    if head[:1] == b"<" and (
        b"<rss" in head or b"<feed" in head or b"<rdf:RDF" in head
    ):
        return "rss"
    return None


LINK_TAG = re.compile(r"<link\b[^>]*>", re.I)
ATTR = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def discover_feeds(html: str, base_url: str) -> list[tuple[str, str]]:
    """Find <link rel="alternate" type="text/calendar|rss|atom"> in an HTML page."""
    found: list[tuple[str, str]] = []
    for tag in LINK_TAG.findall(html):
        attrs = {}
        for name, double, single in ATTR.findall(tag):
            attrs[name.lower()] = double or single
        kind = attrs.get("type", "").lower()
        href = attrs.get("href", "")
        if href and any(kind.startswith(t) for t in FEED_TYPES):
            found.append((kind, urljoin(base_url, href)))
    return found


# ------------------------------------------------------------------- ICS parsing


def _split_key(key: str) -> tuple[str, list[str]]:
    parts = key.split(";")
    return parts[0].strip().upper(), [p.strip() for p in parts[1:]]


# Some servers (typically WordPress sites behind an HTML-minifying cache) emit a
# whole VCALENDAR on one line, with spaces where CRLF should be. The format is
# non-conformant but the data is intact, so recover the boundaries at property
# names and flag it: silent repair would hide a real upstream problem.
PROPERTY_START = re.compile(r"\s+(?=(?:BEGIN|END):[A-Z]+|[A-Z][A-Z0-9-]*(?:;[^:\s]*)*:)")


def ics_is_flattened(text: str) -> bool:
    return "\n" not in text and text.lstrip().startswith("BEGIN:VCALENDAR")


def _refold_flat_ics(text: str) -> str:
    return PROPERTY_START.sub("\n", text.strip())


def _parse_ics_dt(value: str, params: list[str]) -> datetime | None:
    value = value.strip()
    tz = None
    for param in params:
        if param.upper().startswith("TZID="):
            tzid = param.split("=", 1)[1].strip('"')
            try:
                tz = ZoneInfo(tzid)
            except (ZoneInfoNotFoundError, ValueError):
                tz = None
    if value.endswith("Z"):
        try:
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if "T" in value:
        try:
            parsed = datetime.strptime(value, "%Y%m%dT%H%M%S")
        except ValueError:
            return None
        return parsed.replace(tzinfo=tz) if tz else parsed
    try:
        return datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return None


def parse_ics(text: str) -> list[Event]:
    if ics_is_flattened(text):
        text = _refold_flat_ics(text)
    lines: list[str] = []
    for raw in text.splitlines():
        if raw[:1] in (" ", "\t") and lines:  # RFC 5545 line folding
            lines[-1] += raw[1:]
        else:
            lines.append(raw.rstrip("\r"))

    events: list[Event] = []
    props: dict[str, str] | None = None
    for line in lines:
        upper = line.upper()
        if upper.startswith("BEGIN:VEVENT"):
            props = {}
            continue
        if upper.startswith("END:VEVENT"):
            if props:
                title = props.get("SUMMARY", "").strip()
                start = None
                image = ""
                for key, value in props.items():
                    base, params = _split_key(key)
                    if base == "DTSTART":
                        start = _parse_ics_dt(value, params)
                    elif base == "ATTACH" and value.startswith(("http://", "https://")):
                        # Prefer an attachment explicitly typed as an image.
                        if not image or any("image/" in p.upper() for p in params):
                            image = value
                events.append(
                    Event(
                        title=title or "(untitled)",
                        start=start,
                        uid=props.get("UID", "").strip(),
                        location=props.get("LOCATION", "").replace("\\,", ",").strip(),
                        url=props.get("URL", "").strip(),
                        image=image,
                    )
                )
            props = None
            continue
        if props is not None and ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            # Normalise the property name, but keep parameter values (TZID=…) intact:
            # zone keys are case-sensitive-looking to users even if ZoneInfo isn't.
            if ";" in key:
                base, _, params = key.partition(";")
                key = base.strip().upper() + ";" + params
            else:
                key = key.upper()
            props[key] = value.strip()
    return events


# ------------------------------------------------------------------- RSS parsing


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(element, name: str) -> str:
    for sub in element:
        if _local(sub.tag) == name:
            return (sub.text or "").strip()
    return ""


def _parse_date(raw: str) -> datetime | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_rss(text: str) -> list[Event]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []

    events: list[Event] = []
    for element in root.iter():
        if _local(element.tag) not in ("item", "entry"):
            continue
        link = ""
        for sub in element:
            if _local(sub.tag) == "link":
                link = (sub.get("href") or (sub.text or "")).strip()
                break
        start = None
        for field_name in ("pubdate", "published", "updated", "date"):
            raw = _child_text(element, field_name)
            if raw:
                start = _parse_date(raw)
                if start:
                    break
        events.append(
            Event(
                title=_child_text(element, "title") or "(untitled)",
                start=start,
                uid=_child_text(element, "guid") or _child_text(element, "id") or link,
                url=link,
            )
        )
    return events


# ------------------------------------------------------------------- per source


def _as_utc(moment: datetime | None) -> datetime | None:
    """Normalise for comparison. All-day dates (DTSTART;VALUE=DATE) are naive."""
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def is_upcoming(start: datetime | None) -> bool:
    moment = _as_utc(start)
    return moment is not None and moment >= datetime.now(timezone.utc) - timedelta(hours=2)


def read_feed(url: str, timeout: float):
    status, content_type, body, error = fetch(url, timeout)
    if error:
        return None, [], error
    kind = classify(content_type, body)
    if kind == "ics":
        return "ics", parse_ics(decode(body, content_type)), None
    if kind == "rss":
        return "rss", parse_rss(decode(body, content_type)), None
    return None, [], None


def analyse(name: str, url: str, timeout: float, probe: bool) -> Result:
    result = Result(name=name, url=url)

    platform = social_platform(url)
    if platform:
        # Deliberately not fetched: the response is a login wall either way, and
        # the answer is already known.
        result.kind = "social"
        result.status = "SOCIAL"
        result.notes.append(f"{platform}: no feed for third-party accounts; needs Phase 3")
        return result

    status, content_type, body, error = fetch(url, timeout)
    if error:
        result.status = "ERROR"
        result.notes.append(error)
        return result

    kind = classify(content_type, body)
    if kind:
        text = decode(body, content_type)
        if kind == "ics" and ics_is_flattened(text):
            result.notes.append("ICS arrived space-flattened (no line breaks); recovered")
        events = parse_ics(text) if kind == "ics" else parse_rss(text)
        return _score(result, kind, events, url)

    # HTML (or unknown): look for advertised feeds.
    text = decode(body, content_type)
    candidates = discover_feeds(text, url)
    if candidates:
        result.notes.append(f"page advertises {candidates[0][1]}")
        for _kind, href in candidates[:2]:
            sub_kind, events, sub_error = read_feed(href, timeout)
            if sub_kind:
                return _score(result, sub_kind, events, href)
            if sub_error:
                result.notes.append(f"advertised feed failed: {sub_error}")

    if probe:
        for path in PROBE_PATHS:
            href = urljoin(url, path)
            sub_kind, events, _ = read_feed(href, timeout)
            if sub_kind and events:
                result.notes.append(f"probe hit {href}")
                return _score(result, sub_kind, events, href)
            time.sleep(0.5)

    result.notes.append("no ICS/RSS/Atom found" + ("" if probe else "; try --probe"))
    return result


def _score(result: Result, kind: str, events: list[Event], source_url: str) -> Result:
    result.kind = kind
    result.items = len(events)
    if kind == "ics":
        upcoming = [event for event in events if is_upcoming(event.start)]
        result.upcoming = len(upcoming)
        result.status = "WORKS" if upcoming else "EMPTY"
        horizon = datetime.max.replace(tzinfo=timezone.utc)
        for event in sorted(upcoming, key=lambda e: _as_utc(e.start) or horizon)[:3]:
            stamp = event.start.strftime("%Y-%m-%d %H:%M") if event.start else "no date"
            result.samples.append(f"{stamp} {event.title}"[:70])
        if not upcoming:
            result.notes.append("parsed, but no event starts within now-2h..future")
    else:
        dated = [d for d in (_as_utc(event.start) for event in events) if d]
        result.newest = max(dated) if dated else None
        result.status = "WORKS" if events else "EMPTY"
        for event in events[:3]:
            result.samples.append(event.title[:70])
        result.notes.append("RSS: dates are publication times; event dates must be parsed from text")
    if source_url != result.url:
        result.notes.append(f"via {source_url}")
    return result


# ------------------------------------------------------------------------ input


def read_sources(path: str) -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []
    try:
        handle = open(path, encoding="utf-8")
    except FileNotFoundError:
        # The real list is personal and not committed, so a fresh clone has no
        # sources.txt. Say what to do instead of raising a traceback.
        raise SystemExit(
            f"sources file not found: {path}\n"
            "Your watch list is private and is not shipped with the repo. Create it:\n"
            "    cp sources.example.txt sources.txt"
        )
    with handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                name, url = (part.strip() for part in line.split("|", 1))
            else:
                name, url = line, line
            if not url.lower().startswith(("http://", "https://")):
                print(f"sources:{number}: skipping non-URL line", file=sys.stderr)
                continue
            sources.append((name, url))
    return sources


# ---------------------------------------------------------------------- reporting


def truncate(value: str, width: int) -> str:
    return value if len(value) <= width else value[: width - 1] + "…"


def report(results: list[Result], as_json: str | None) -> int:
    if as_json:
        payload = [
            {
                "name": item.name,
                "url": item.url,
                "kind": item.kind,
                "status": item.status,
                "items": item.items,
                "upcoming": item.upcoming,
                "newest": item.newest.isoformat() if item.newest else None,
                "samples": item.samples,
                "notes": item.notes,
            }
            for item in results
        ]
        with open(as_json, "w", encoding="utf-8") as handle:
            jsonlib.dump(payload, handle, indent=2, ensure_ascii=False)
        print(f"wrote {as_json}")

    name_width = max([len(truncate(item.name, 28)) for item in results] + [6])
    print()
    print(f"{'SOURCE'.ljust(name_width)}  TYPE  STATUS  ITEMS  SOON  DETAIL")
    print("-" * (name_width + 46))
    for item in results:
        detail = "; ".join(item.samples[:1] + item.notes[:1])
        print(
            f"{truncate(item.name, 28).ljust(name_width)}  "
            f"{(item.kind or '-').ljust(4)}  "
            f"{item.status.ljust(6)}  "
            f"{str(item.items).rjust(5)}  "
            f"{str(item.upcoming).rjust(4)}  "
            f"{truncate(detail, 46)}"
        )

    total = len(results)
    failed = [item for item in results if item.status == "ERROR"]
    social = [item for item in results if item.status == "SOCIAL"]
    feeds = [item for item in results if item.status not in ("ERROR", "SOCIAL")]
    works = sum(1 for item in feeds if item.status == "WORKS")
    readable = sum(1 for item in feeds if item.kind in ("ics", "rss"))

    print()
    if failed:
        print(f"{len(failed)}/{total} sources could not be fetched (network/TLS/blocked) — excluded from the verdict.")
    if social:
        print(
            f"{len(social)}/{total} social source(s) excluded: third-party accounts have no readable "
            "feed (Phase 3, by definition — not a failure of these sources)."
        )

    if not feeds:
        if social:
            print("Verdict: nothing to build a feed pipeline on. Social collection is the critical path —")
            print("         run the Instaloader 24h test next (3 accounts, count 401/429).")
            return 0
        print("Verdict: inconclusive. Nothing was fetched, so nothing was learned about feed coverage.")
        if any("CERTIFICATE_VERIFY_FAILED" in " ".join(item.notes) for item in results):
            print("Cause looks like a missing CA bundle for this Python, not a dead source.")
            print("Try: SSL_CERT_FILE=/etc/ssl/cert.pem python3 audit.py sources.txt")
        print("         Fix connectivity/TLS and re-run before drawing conclusions.")
        return 2

    print(f"{works}/{len(feeds)} web sources yield usable content, {readable}/{len(feeds)} are machine-readable.")

    structured = sum(1 for item in feeds if item.kind == "ics")
    articles = sum(1 for item in feeds if item.kind == "rss")
    print(f"  {structured} ICS with real event dates, {articles} RSS/article feeds (dates must be extracted).")

    if len(feeds) < 3:
        print(f"Note: only {len(feeds)} web source(s) evaluated — too few to conclude anything about coverage.")

    ratio = readable / len(feeds)
    if ratio >= 0.75 and structured >= max(1, len(feeds) // 2):
        print("Verdict: feed-first is viable and mostly structured. Build Phase 1 as designed.")
    elif ratio >= 0.75:
        print("Verdict: feeds exist, but they are article feeds, not event calendars.")
        print("         Phase 1 still avoids scraping, but must extract event dates from the text.")
    elif ratio <= 0.5:
        print("Verdict: feeds are not enough. Web sources must be scraped or converted; social needs Phase 3.")
    else:
        print("Verdict: mixed. Build Phase 1 for the readable half, then fill gaps with RSS-Bridge")
        print("         before considering any authenticated scraping.")
    return 0


# ----------------------------------------------------------------------- selftest

ICS_FIXTURE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Eventure//selftest//EN
BEGIN:VEVENT
UID:abc-123
DTSTART;TZID=Europe/Lisbon:20261201T200000
SUMMARY:Live Jazz Night at the
  old warehouse
LOCATION:Somewhere\\, City
ATTACH;FMTTYPE=image/jpeg:https://example.org/poster.jpg
END:VEVENT
BEGIN:VEVENT
UID:def-456
DTSTART:20261125T190000Z
SUMMARY:Art Opening
END:VEVENT
END:VCALENDAR
"""

# A real-world shape: a minifying cache collapsed the whole calendar onto one line.
ICS_FLAT_FIXTURE = (
    "BEGIN:VCALENDAR VERSION:2.0 PRODID:-//Mangled//EN "
    "BEGIN:VEVENT UID:flat-1 DTSTART;TZID=Europe/Lisbon:20261201T200000 "
    "SUMMARY:Flat Feed Event END:VEVENT END:VCALENDAR"
)

RSS_FIXTURE = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>Venue</title>
<item><title>New season announced</title><link>https://example.org/a</link>
<guid>a1</guid><pubDate>Tue, 10 Nov 2026 18:00:00 +0000</pubDate></item>
<item><title>Late bar Fridays</title><link>https://example.org/b</link>
<guid>b1</guid><pubDate>Mon, 02 Nov 2026 09:30:00 +0000</pubDate></item>
</channel></rss>
"""

ATOM_FIXTURE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Gallery</title>
<entry><title>Open call</title><id>urn:x:1</id>
<link rel="alternate" href="https://example.org/c"/>
<updated>2026-11-15T12:00:00Z</updated></entry>
</feed>
"""

HTML_FIXTURE = """<html><head>
<link rel="alternate" type="text/calendar" href="/events.ics">
<link rel="alternate" type="application/rss+xml" href="/feed">
</head><body>hello</body></html>"""


def selftest() -> int:
    failures: list[str] = []

    ics = parse_ics(ICS_FIXTURE)
    if len(ics) != 2:
        failures.append(f"ics: expected 2 events, got {len(ics)}")
    else:
        if "old warehouse" not in ics[0].title:
            failures.append("ics: folded SUMMARY line not unfolded")
        if ics[0].start is None or ics[0].start.hour != 20:
            failures.append("ics: DTSTART with TZID not parsed")
        elif str(getattr(ics[0].start, "tzinfo", "")) != "Europe/Lisbon":
            failures.append(f"ics: TZID not applied ({ics[0].start.tzinfo!r})")
        elif ics[0].start.utcoffset() != ZoneInfo("Europe/Lisbon").utcoffset(ics[0].start):
            failures.append("ics: TZID offset does not match zone rules")
        if ics[1].start is None or ics[1].start.tzinfo is not timezone.utc:
            failures.append("ics: UTC DTSTART not parsed as UTC")
        if ics[0].location != "Somewhere, City":
            failures.append(f"ics: escaped LOCATION not unescaped ({ics[0].location!r})")
        if ics[0].image != "https://example.org/poster.jpg":
            failures.append(f"ics: ATTACH image not captured ({ics[0].image!r})")

    flat = parse_ics(ICS_FLAT_FIXTURE)
    if not ics_is_flattened(ICS_FLAT_FIXTURE):
        failures.append("ics: space-flattened calendar not detected")
    if len(flat) != 1 or flat[0].title != "Flat Feed Event":
        failures.append(f"ics: flattened calendar not repaired ({[e.title for e in flat]})")
    elif flat[0].start is None or flat[0].start.hour != 20:
        failures.append("ics: flattened DTSTART not parsed")

    # All-day dates are naive; timed ones are aware. Sorting must not mix them.
    mixed = [
        Event("all-day", datetime(2026, 12, 1, 0, 0)),
        Event("timed", datetime(2026, 12, 2, 10, 0, tzinfo=timezone.utc)),
    ]
    try:
        _score(Result(name="x", url="x"), "ics", mixed, "x")
    except TypeError:
        failures.append("ics: naive and aware starts crash the upcoming sort")

    rss = parse_rss(RSS_FIXTURE)
    if len(rss) != 2 or rss[0].title != "New season announced":
        failures.append(f"rss: unexpected parse ({[e.title for e in rss]})")
    elif rss[0].start is None or rss[0].start.year != 2026:
        failures.append("rss: pubDate not parsed")

    atom = parse_rss(ATOM_FIXTURE)
    if len(atom) != 1 or atom[0].uid != "urn:x:1":
        failures.append(f"atom: unexpected parse ({atom})")

    feeds = discover_feeds(HTML_FIXTURE, "https://example.org/events/")
    if [kind for kind, _ in feeds] != ["text/calendar", "application/rss+xml"]:
        failures.append(f"html: feed discovery wrong ({feeds})")
    elif feeds[0][1] != "https://example.org/events.ics":
        failures.append(f"html: relative href not resolved ({feeds[0][1]})")

    if classify("text/calendar; charset=utf-8", b"BEGIN:VCALENDAR") != "ics":
        failures.append("classify: calendar content-type not detected")

    if social_platform("https://www.instagram.com/some_profile?utm=x") != "instagram":
        failures.append("social: instagram not detected")
    if social_platform("https://www.facebook.com/share/abc/") != "facebook":
        failures.append("social: facebook share link not detected")
    if social_platform("https://notinstagram.com/foo") is not None:
        failures.append("social: suffix match too loose")
    if social_platform("https://example.org/eventos/") is not None:
        failures.append("social: false positive on a plain site")

    if failures:
        for failure in failures:
            print(f"FAIL {failure}")
        return 1
    print("selftest ok: ics (folded lines, TZID, UTC, escapes), rss, atom, html discovery, classify")
    return 0


# -------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit sources for machine-readable event feeds.")
    parser.add_argument("sources", nargs="?", help="file with one source per line ('name | url')")
    parser.add_argument("--json", metavar="PATH", help="write a JSON report")
    parser.add_argument("--probe", action="store_true", help="try common feed paths when a page has none")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between sources (default 1.0)")
    parser.add_argument("--timeout", type=float, default=20.0, help="per-request timeout (default 20s)")
    parser.add_argument("--selftest", action="store_true", help="run parser selftests and exit")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.sources:
        parser.error("provide a sources file, or use --selftest")

    sources = read_sources(args.sources)
    if not sources:
        print(f"no sources in {args.sources}", file=sys.stderr)
        return 1

    results: list[Result] = []
    for index, (name, url) in enumerate(sources):
        if index:
            time.sleep(args.delay)
        print(f"[{index + 1}/{len(sources)}] {name} …", file=sys.stderr, flush=True)
        results.append(analyse(name, url, args.timeout, args.probe))

    return report(results, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
