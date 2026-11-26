"""Core types.

Two kinds of findings, deliberately kept apart:

    Event   a dated thing you can put in a calendar
    Signal  a link that might be worth a look, with no trustworthy date

Conflating them is how "discovery" silently hides inside "watching sources".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Event:
    uid: str
    title: str
    start: datetime
    end: datetime | None = None
    location: str = ""
    url: str = ""
    image: str = ""
    source: str = ""
    all_day: bool = False
    # The account a social post came from (@venue). Empty for structured feeds,
    # where the source name ("Durango eventos") already says who it is.
    channel: str = ""
    # "confirmed" = structured data (ICS/JSON). "candidate" = a date guessed
    # from free text (an Instagram caption). The digest shows the difference.
    confidence: str = "confirmed"

    def day_key(self) -> str:
        return self.start.date().isoformat()


@dataclass
class Signal:
    uid: str
    title: str
    url: str
    source: str
    published: datetime | None = None
    note: str = ""
    channel: str = ""


@dataclass
class SourceHealth:
    name: str
    ok: bool
    items: int = 0
    error: str = ""
    baseline: int = 0

    @property
    def suspicious(self) -> bool:
        return self.ok and self.baseline >= 5 and self.items == 0


@dataclass
class Collected:
    events: list[Event] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    health: list[SourceHealth] = field(default_factory=list)

    def extend(self, other: "Collected") -> None:
        self.events.extend(other.events)
        self.signals.extend(other.signals)
        self.health.extend(other.health)
