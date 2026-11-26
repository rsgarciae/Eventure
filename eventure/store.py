"""State: a single JSON file, written atomically once per run.

No database server, no write amplification on the SD card. Recreated freely from
the sources if lost — its only job is remembering what was already shown.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from .model import Event, SourceHealth

VERSION = 1


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def fuzzy_key(event: Event) -> str:
    """Same event published by two sources should collapse to one key."""
    material = f"{_norm(event.title)}|{event.start.date().isoformat()}|{_norm(event.location)}"
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


def _refresh(record: dict, event: Event, fuzzy: str | None = None) -> None:
    """Take the newest data from a source, keeping ids and the user's flags.

    Without this an event first seen before a field was collected (an image, the
    posting account) would stay incomplete for as long as it lives in state.
    """
    record["title"] = event.title
    record["start"] = event.start.isoformat()
    record["end"] = event.end.isoformat() if event.end else None
    record["location"] = event.location
    record["url"] = event.url
    record["image"] = event.image
    record["channel"] = event.channel
    record["all_day"] = event.all_day
    record["confidence"] = event.confidence
    if fuzzy:
        record["fuzzy"] = fuzzy


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = {"version": VERSION, "events": {}, "short": {}, "sources": {}}

    @classmethod
    def load(cls, path: Path) -> "Store":
        store = cls(path)
        if store.path.exists():
            try:
                loaded = json.loads(store.path.read_text(encoding="utf-8"))
                if loaded.get("version") == VERSION:
                    store.data = loaded
            except (json.JSONDecodeError, OSError):
                # Corrupt state is not fatal: worst case you see a digest twice.
                pass
        return store

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ ingest

    def ingest(self, events: list[Event], now: datetime) -> list[Event]:
        """Return only genuinely new events; record them as pending notification."""
        known_uid = self.data["events"]
        seen_fuzzy = {
            rec.get("fuzzy"): uid for uid, rec in known_uid.items() if rec.get("fuzzy")
        }
        fresh: list[Event] = []
        for event in sorted(events, key=lambda item: item.start):
            key = fuzzy_key(event)
            known = known_uid.get(event.uid)
            if known is not None:
                old_key = known.get("fuzzy")
                if old_key and old_key != key:
                    # The event moved: free the old slot, otherwise a different
                    # event on the old date is mistaken for a duplicate.
                    seen_fuzzy.pop(old_key, None)
                seen_fuzzy[key] = event.uid
                _refresh(known, event, key)
                continue
            if key in seen_fuzzy:
                # The same event under a different uid (another source, or the
                # same post once its uid gained a date suffix). Keep the record
                # that already exists and its short id, but take the new data.
                _refresh(known_uid[seen_fuzzy[key]], event)
                continue
            short = hashlib.sha1(event.uid.encode("utf-8")).hexdigest()[:8]
            while short in self.data["short"] and self.data["short"][short] != event.uid:
                short = hashlib.sha1(short.encode("utf-8")).hexdigest()[:8]
            record = {
                "short": short,
                "fuzzy": key,
                "title": event.title,
                "start": event.start.isoformat(),
                "end": event.end.isoformat() if event.end else None,
                "location": event.location,
                "url": event.url,
                "image": event.image,
                "source": event.source,
                "channel": event.channel,
                "all_day": event.all_day,
                "confidence": event.confidence,
                "first_seen": now.isoformat(),
                "notified_at": None,
                "added_at": None,
                "skipped_at": None,
                "gcal_id": None,
            }
            known_uid[event.uid] = record
            self.data["short"][short] = event.uid
            seen_fuzzy[key] = event.uid
            fresh.append(event)
        return fresh

    def pending(self) -> list[dict]:
        return [
            record
            for record in self.data["events"].values()
            if record.get("notified_at") is None and record.get("skipped_at") is None
        ]

    def mark_notified(self, events: list[Event], now: datetime) -> None:
        for event in events:
            record = self.data["events"].get(event.uid)
            if record is not None:
                record["notified_at"] = now.isoformat()

    def reset_notified(self, include_added: bool = False) -> tuple[int, int]:
        """Forget what was already shown, so the next digest repeats it.

        Events already added to the calendar or skipped by hand are left alone:
        re-sending those would offer to add the same event a second time. Pass
        ``include_added`` to replay them anyway — their button then answers
        "already added" instead of writing a duplicate.
        """
        events = 0
        for record in self.data["events"].values():
            if not include_added and (record.get("added_at") or record.get("skipped_at")):
                continue
            if record.get("notified_at"):
                record["notified_at"] = None
                events += 1
        signals = len(self.data.get("signals", {}))
        self.data["signals"] = {}
        return events, signals

    def by_short(self, short: str) -> tuple[str, dict] | None:
        uid = self.data["short"].get(short)
        if not uid:
            return None
        return uid, self.data["events"][uid]

    def mark_added(self, uid: str, gcal_id: str, now: datetime) -> None:
        record = self.data["events"].get(uid)
        if record is not None:
            record["added_at"] = now.isoformat()
            record["gcal_id"] = gcal_id

    def mark_skipped(self, uid: str, now: datetime) -> None:
        record = self.data["events"].get(uid)
        if record is not None:
            record["skipped_at"] = now.isoformat()

    # ------------------------------------------------------------------ health

    def record_health(self, health: list[SourceHealth]) -> None:
        checked_at = datetime.now().astimezone().isoformat()
        for item in health:
            previous = self.data["sources"].get(item.name, {})
            baseline = previous.get("baseline_items", 0)
            if item.ok:
                # Roll with hysteresis so one quiet run does not look like breakage.
                baseline = max(0, int(baseline * 0.7) + int(item.items * 0.3))
                self.data["sources"][item.name] = {
                    "checked_at": checked_at,
                    "last_success": checked_at,
                    "last_items": item.items,
                    "baseline_items": baseline,
                    "consecutive_failures": 0,
                    "last_error": item.error if item.error else "",
                }
            else:
                self.data["sources"][item.name] = {
                    **previous,
                    "checked_at": checked_at,
                    "consecutive_failures": int(previous.get("consecutive_failures", 0)) + 1,
                    "last_error": item.error,
                }

    def health_summary(self) -> tuple[int, int, list[str]]:
        sources = self.data["sources"]
        failing = [name for name, rec in sources.items() if rec.get("consecutive_failures", 0) > 0]
        return len(sources) - len(failing), len(sources), failing

    def prune(self, days: int = 120) -> None:
        cutoff = datetime.now().astimezone() - timedelta(days=days)
        for uid, record in list(self.data["events"].items()):
            try:
                start = datetime.fromisoformat(record["start"])
            except (KeyError, ValueError):
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=cutoff.tzinfo)
            if start < cutoff:
                self.data["events"].pop(uid, None)
                self.data["short"].pop(record.get("short", ""), None)
