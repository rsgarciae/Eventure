"""Source adapters.

ICS and RSS parsing is reused from audit.py on purpose: one parser, one set of
bug fixes, no second implementation to drift.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import audit

from . import dates
from .config import Config, Source
from .model import Collected, Event, Signal, SourceHealth

TIMEOUT = 25.0
MAX_SIGNALS_PER_SOURCE = 5


def _fallback_uid(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:24]


def _as_local(moment: datetime, tz) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=tz)


def collect(cfg: Config) -> Collected:
    """Collect every source. One broken source never aborts the run."""
    total = Collected()
    for source in cfg.sources:
        if source.type == "instagram_profile":
            continue  # fetched by ig_collect.py, then read back as instagram_json
        try:
            total.extend(_collect_one(source, cfg))
        except Exception as exc:  # noqa: BLE001 - isolate per source
            total.health.append(
                SourceHealth(name=source.name, ok=False, error=f"{type(exc).__name__}: {exc}")
            )
    return total


def _collect_one(source: Source, cfg: Config) -> Collected:
    if source.type == "ics":
        return _from_ics(source, cfg)
    if source.type == "rss":
        return _from_rss(source, cfg)
    if source.type == "instagram_json":
        return _from_instagram(source, cfg)
    raise ValueError(f"unsupported source type {source.type!r}")


def _window(cfg: Config) -> tuple[datetime, datetime]:
    now = datetime.now(cfg.tz)
    return now - timedelta(hours=cfg.past_grace_hours), now + timedelta(days=cfg.horizon_days)


def _from_ics(source: Source, cfg: Config) -> Collected:
    status, content_type, body, error = audit.fetch(source.url, TIMEOUT)
    if error:
        raise RuntimeError(error)
    if audit.classify(content_type, body) != "ics":
        raise RuntimeError(f"not a calendar (content-type {content_type or 'unknown'})")

    text = audit.decode(body, content_type)
    repaired = audit.ics_is_flattened(text)
    raw = audit.parse_ics(text)

    start_floor, start_ceiling = _window(cfg)
    events: list[Event] = []
    for item in raw:
        if item.start is None:
            continue
        start = _as_local(item.start, cfg.tz)
        if not (start_floor <= start <= start_ceiling):
            continue
        events.append(
            Event(
                uid=item.uid or _fallback_uid(source.name, item.title, start.isoformat()),
                title=item.title,
                start=start,
                location=item.location,
                url=item.url,
                image=item.image,
                source=source.name,
                all_day=item.start.tzinfo is None and (item.start.hour, item.start.minute) == (0, 0),
            )
        )

    health = SourceHealth(name=source.name, ok=True, items=len(events))
    if repaired:
        health.error = "note: feed arrived space-flattened; recovered"
    return Collected(events=events, health=[health])


def _from_rss(source: Source, cfg: Config) -> Collected:
    status, content_type, body, error = audit.fetch(source.url, TIMEOUT)
    if error:
        raise RuntimeError(error)

    text = audit.decode(body, content_type)
    items = audit.parse_rss(text)
    signals: list[Signal] = []
    for item in items[:MAX_SIGNALS_PER_SOURCE]:
        signals.append(
            Signal(
                uid=item.uid or _fallback_uid(source.name, item.title),
                title=item.title,
                url=item.url,
                source=source.name,
                published=_as_local(item.start, cfg.tz) if item.start else None,
                note="RSS: no event date in the feed",
            )
        )
    return Collected(
        signals=signals,
        health=[SourceHealth(name=source.name, ok=True, items=len(signals))],
    )


def _from_instagram(source: Source, cfg: Config) -> Collected:
    path = Path(source.path or "data/instagram.json")
    if not path.exists():
        raise RuntimeError(f"{path} missing — run ig_collect.py first")
    payload = json.loads(path.read_text(encoding="utf-8"))
    posts = payload.get("posts", payload) if isinstance(payload, dict) else payload
    errors = payload.get("errors", []) if isinstance(payload, dict) else []

    start_floor, start_ceiling = _window(cfg)
    now = datetime.now(cfg.tz)
    events: list[Event] = []
    signals: list[Signal] = []

    for post in posts[: MAX_SIGNALS_PER_SOURCE * 4]:
        caption = str(post.get("caption") or "")
        url = str(post.get("url") or "")
        title = " ".join(caption.split())[:90] or "(sin texto)"
        base_uid = post.get("uid") or _fallback_uid(source.name, url, caption[:40])
        channel = str(post.get("profile") or "")

        # One post can announce several nights; each one is its own event so the
        # ➕ button adds the night you actually meant.
        found_all = dates.find_all(
            caption, cfg.tz, now, cfg.date_order, (cfg.language, "en")
        )
        upcoming = [
            found for found in found_all if start_floor <= found.start <= start_ceiling
        ]

        if upcoming:
            for found in upcoming:
                # A single-date post keeps its original uid, so records and
                # buttons created before multi-date support keep resolving. A
                # multi-date post gets a stable uid per date instead of handing
                # the original uid to whichever date happens to sort first.
                uid = (
                    base_uid
                    if len(upcoming) == 1
                    else f"{base_uid}#{found.start:%Y-%m-%d}"
                )
                events.append(
                    Event(
                        uid=uid,
                        title=title,
                        start=found.start,
                        url=url,
                        image=str(post.get("image") or ""),
                        source=source.name,
                        channel=channel,
                        all_day=not found.has_time,
                        confidence="candidate",
                    )
                )
            continue

        signals.append(
            Signal(
                uid=base_uid,
                title=title,
                url=url,
                source=source.name,
                channel=channel,
                # Say which it is: a post with a date outside the horizon is not
                # the same as a post with no detectable date.
                note="date outside horizon" if found_all else "no date found",
            )
        )

    # Partial failure is still a failure: say so, do not silently show fewer posts.
    error_text = "; ".join(
        f"{item.get('profile', '?')}: {item.get('error', 'failed')}" for item in errors[:3]
    )
    return Collected(
        events=events,
        signals=signals[:MAX_SIGNALS_PER_SOURCE],
        health=[
            SourceHealth(
                name=source.name,
                ok=not errors,
                items=len(events) + len(signals),
                error=error_text,
            )
        ],
    )
