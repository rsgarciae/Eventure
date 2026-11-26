"""Dates in free text, and human formatting.

Only used for *candidates* (Instagram captions, RSS titles). Structured sources
never go through this. Anything found here is labelled "candidate" in the digest
so a guess is never presented as a fact.

Month and weekday names are looked up by language, never assumed. Unknown
languages fall back to ISO/numeric formatting, which is still correct.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta

_MONTH_NAMES = {
    "es": [
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    ],
    "en": [
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    ],
}

_MONTH_OUT = {
    "es": ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"],
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
}

_WEEKDAY_OUT = {
    "es": ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"],
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
}

# Inbound weekday names, accent-free because they are matched after _fold().
_WEEKDAY_IN = {
    "lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3,
    "viernes": 4, "sabado": 5, "domingo": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def _month_lookup(languages: tuple[str, ...]) -> dict[str, int]:
    table: dict[str, int] = {}
    for language in languages:
        names = _MONTH_NAMES.get(language, ())
        for index, name in enumerate(names, start=1):
            table[name] = index
            table[name[:3]] = index
    return table


@dataclass(frozen=True)
class Extracted:
    start: datetime
    has_time: bool


# "9:00 pm", "9:00pm", "9:00 p. m.", "9:00 de la noche", "9:00 de la tarde"
_PM = re.compile(r"\s*(?:p\.?\s?m\.?|de\s+la\s+(?:tarde|noche))")
# "9:00 am", "9:00 a. m.", "9:00 de la manana", "9:00 de la madrugada"
# (accents are already folded away by _fold)
_AM = re.compile(r"\s*(?:a\.?\s?m\.?|de\s+la\s+(?:manana|madrugada))")


def _adjust(hour: int, minute: int, after: str) -> tuple[int, int]:
    if _PM.match(after):
        if hour < 12:
            hour += 12
    elif _AM.match(after):
        if hour == 12:
            hour = 0
    return hour, minute


def _clocks(folded: str) -> list[tuple[int, int]]:
    """Every clock time in the text, in reading order, converted to 24 h.

    The marker has to be read from the text after the digits: captions say
    "arrancamos en punto de las 9:00 pm", and dropping the ``pm`` silently
    turns a 21:00 event into a 09:00 one.
    """
    found: list[tuple[int, int]] = []
    for match in re.finditer(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", folded):
        after = folded[match.end():match.end() + 16]
        found.append(_adjust(int(match.group(1)), int(match.group(2)), after))
    if found:
        return found
    # No minutes: only accept it when an explicit marker follows, otherwise a
    # day number such as "28 de noviembre" would read as an hour.
    for match in re.finditer(
        r"\b([01]?\d|2[0-3])\s*(?:hrs?|h)?\s*"
        r"(a\.?\s?m\.?|p\.?\s?m\.?|de\s+la\s+(?:tarde|noche|manana|madrugada))\b",
        folded,
    ):
        # The marker was consumed by the match, so put it back in front.
        after = " " + match.group(2) + folded[match.end():match.end() + 16]
        found.append(_adjust(int(match.group(1)), 0, after))
    return found


def _clock(folded: str) -> tuple[int, int] | None:
    clocks = _clocks(folded)
    return clocks[0] if clocks else None


def find_all(
    text: str,
    tz,
    reference: datetime,
    date_order: str = "DMY",
    languages: tuple[str, ...] = ("es", "en"),
) -> list[Extracted]:
    """Every plausible date in ``text``, earliest first.

    A single post can announce several nights ("viernes 27 y sábado 28 de
    noviembre"), and each one is its own event: same caption, different day.
    """
    if not text:
        return []
    folded = _fold(text)
    months = _month_lookup(languages)
    days: set[tuple[int, int, int]] = set()
    explicit_days: set[int] = set()

    def add(month: int, day: int, year: int | None = None) -> None:
        if not (1 <= day <= 31 and 1 <= month <= 12):
            return
        explicit_days.add(day)
        if year is None:
            year = reference.year
            probe = _build(year, month, day, 0, 0, tz)
            if probe is not None and probe.date() < reference.date() - timedelta(days=1):
                year += 1
        if _build(year, month, day, 0, 0, tz) is not None:
            days.add((year, month, day))

    for match in re.finditer(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", folded):
        add(int(match.group(2)), int(match.group(3)), int(match.group(1)))

    for match in re.finditer(
        r"\b(\d{1,2})\s+de\s+([a-z]{3,12})(?:\s+de\s+(20\d{2}))?\b", folded
    ):
        if match.group(2) in months:
            add(months[match.group(2)], int(match.group(1)),
                int(match.group(3)) if match.group(3) else None)

    for match in re.finditer(r"\b(\d{1,2})\s+([a-z]{3,9})(?:\s+(20\d{2}))?\b", folded):
        if match.group(2) in months:
            add(months[match.group(2)], int(match.group(1)),
                int(match.group(3)) if match.group(3) else None)

    for match in re.finditer(r"\b(\d{1,2})[/.](\d{1,2})(?:[/.](20\d{2}))?\b", folded):
        first, second = int(match.group(1)), int(match.group(2))
        year = int(match.group(3)) if match.group(3) else None
        # Follow the configured order; only flip when that reading is impossible
        # (e.g. 25/11 cannot be month/day in MDY).
        if date_order.upper() == "MDY":
            month, day = first, second
            if month > 12 and day <= 12:
                month, day = day, month
        else:
            day, month = first, second
            if month > 12 and day <= 12:
                day, month = month, day
        add(month, day, year)

    # "este jueves 26": a weekday next to a day number, with no month at all.
    for match in re.finditer(r"\b(" + "|".join(_WEEKDAY_IN) + r")\s+(\d{1,2})\b", folded):
        day, target = int(match.group(2)), _WEEKDAY_IN[match.group(1)]
        # An explicit "28 de noviembre" already settles that day. Only guess a
        # month from the weekday when no caption month names it, otherwise a
        # caption that gets its own weekday wrong yields a phantom second date.
        if day in explicit_days:
            continue
        # Walk forward a few months and keep the first day that both exists and
        # lands on the named weekday. No match means no event: better a signal
        # than a wrong date.
        for offset in range(3):
            month = (reference.month - 1 + offset) % 12 + 1
            year = reference.year + (reference.month - 1 + offset) // 12
            probe = _build(year, month, day, 0, 0, tz)
            if probe is None or probe.weekday() != target:
                continue
            if probe.date() < reference.date() - timedelta(days=1):
                continue
            days.add((year, month, day))
            break

    clocks = _clocks(folded)
    # One time and one date is unambiguous. Several dates sharing one time is
    # still fine. Several times over several dates is not: leave those all-day
    # rather than attach the wrong hour to the wrong night.
    if len(clocks) == 1 or (clocks and len(days) <= 1):
        hour, minute, timed = clocks[0][0], clocks[0][1], True
    else:
        hour, minute, timed = 0, 0, False

    results = []
    for year, month, day in sorted(days):
        moment = _build(year, month, day, hour, minute, tz)
        if moment is not None:
            results.append(Extracted(start=moment, has_time=timed))
    results.sort(key=lambda item: item.start)
    return results


def find(
    text: str,
    tz,
    reference: datetime,
    date_order: str = "DMY",
    languages: tuple[str, ...] = ("es", "en"),
) -> Extracted | None:
    """Return the first plausible date mentioned in ``text``."""
    found = find_all(text, tz, reference, date_order, languages)
    return found[0] if found else None


def _build(year: int, month: int, day: int, hour: int, minute: int, tz) -> datetime | None:
    try:
        return datetime(year, month, day, hour, minute, tzinfo=tz)
    except ValueError:
        return None  # e.g. 31 February: a guess, not an event


def format_day(moment: datetime, language: str) -> str:
    weekdays = _WEEKDAY_OUT.get(language)
    months = _MONTH_OUT.get(language)
    if not weekdays or not months:
        return moment.strftime("%Y-%m-%d")
    return f"{weekdays[moment.weekday()]} {moment.day} {months[moment.month - 1]}"


def format_when(moment: datetime, language: str, all_day: bool = False) -> str:
    day = format_day(moment, language)
    return day if all_day else f"{day} · {moment:%H:%M}"
