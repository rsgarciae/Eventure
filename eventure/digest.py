"""Digest rendering. Labels come from the configured language, never hardcoded
into the flow logic; an unknown language falls back to English formatting.
"""

from __future__ import annotations

import html
from datetime import datetime

from . import dates
from .config import Config
from .model import Event, Signal, SourceHealth

LABELS = {
    "es": {
        "web": "Páginas web",
        "instagram": "Instagram",
        "events": "Eventos",
        "signals": "Pistas (sin fecha fiable)",
        "quiet": "Sin novedades esta vez.",
        "sources": "Fuentes: {ok}/{total} OK",
        "failing": "con fallos: {names}",
        "candidate": "fecha estimada",
        "help": "✅ Eventure está conectado.\n\nTe enviaré un digest con los próximos eventos; cada uno trae un botón ➕ para añadirlo a tu calendario.",
        "not_found": "evento no encontrado",
        "already_added": "ya estaba en tu calendario",
        "no_calendar": "Google Calendar no está configurado",
        "add_failed": "error al añadir; revisa los logs",
        "added": "añadido al calendario ✓",
        "add": "Añadir al calendario",
        "more": "… y {n} más (no caben en el mensaje; usa `ics` para el calendario completo)",
        "broken": "⚠️ La recolección falló por completo. Esto NO significa que no haya eventos.",
        "broken_hint": "Revisa las fuentes antes de asumir que la semana está vacía.",
    },
    "en": {
        "web": "Websites",
        "instagram": "Instagram",
        "events": "Events",
        "signals": "Signals (no reliable date)",
        "quiet": "Nothing new this time.",
        "sources": "Sources: {ok}/{total} OK",
        "failing": "failing: {names}",
        "candidate": "guessed date",
        "help": "✅ Eventure is connected.\n\nYou will get a digest of upcoming events; each one has a ➕ button to add it to your calendar.",
        "not_found": "event not found",
        "already_added": "it was already in your calendar",
        "no_calendar": "Google Calendar is not configured",
        "add_failed": "could not add it; check the logs",
        "added": "added to your calendar ✓",
        "add": "Add to calendar",
        "more": "… and {n} more (does not fit; use `ics` for the full calendar)",
        "broken": "⚠️ Collection failed entirely. This does NOT mean there are no events.",
        "broken_hint": "Check the sources before assuming the week is empty.",
    },
}


def labels(cfg: Config) -> dict:
    return LABELS.get(cfg.language, LABELS["en"])


def _esc(value: str) -> str:
    return html.escape(value or "", quote=False)


def _shorten(value: str, limit: int) -> str:
    """Cut on a word boundary; a street cut mid-word reads like a typo."""
    if len(value) <= limit:
        return value
    cut = value[:limit].rsplit(" ", 1)[0] or value[:limit]
    return cut.rstrip(",;· ") + "…"


def _event_block(event: Event, cfg: Config) -> list[str]:
    mark = "" if event.confidence == "confirmed" else " ⚠"
    meta = dates.format_when(event.start, cfg.language, event.all_day)
    if event.channel:
        # A caption from @venue: the handle is what tells you where to go.
        meta += f" · @{_esc(event.channel)}"
    if event.location:
        meta += f" · {_esc(_shorten(event.location, 70))}"
    block = ["", f"<b>{_esc(event.title)}</b>{mark}", meta]
    if event.url:
        block.append(_esc(event.url))
    return block


_REASON_TOKENS = (
    ("feedback_required", "feedback_required"),
    ("401", "401"),
    ("403", "403"),
    ("429", "429"),
    ("missing", "missing"),
    ("not a calendar", "not-a-calendar"),
)


def _reason(error: str) -> str:
    """Short, language-neutral code so the footer says *why* a source is red."""
    lowered = (error or "").lower()
    for needle, label in _REASON_TOKENS:
        if needle in lowered:
            return label
    return ""


def _footer(health: list[SourceHealth], cfg: Config) -> str:
    L = labels(cfg)
    ok = sum(1 for item in health if item.ok)
    line = L["sources"].format(ok=ok, total=len(health))
    failing = []
    for item in health:
        if item.ok:
            continue
        reason = _reason(item.error)
        failing.append(f"{item.name} ({reason})" if reason else item.name)
    if failing:
        line += " · " + L["failing"].format(names=", ".join(failing[:4]))
    return f"<i>{_esc(line)}</i>"


def render(
    items: list[tuple[Event, str]],
    signals: list[Signal],
    health: list[SourceHealth],
    cfg: Config,
    section: str = "",
) -> tuple[str, list[list[dict]]]:
    """Fit as many whole events as possible into one message.

    Never truncate mid-event: a half-rendered title with no button is worse than
    not showing it, so the overflow is counted out loud instead.
    """
    L = labels(cfg)
    today = dates.format_day(datetime.now(cfg.tz), cfg.language).capitalize()
    header = [f"🗓 <b>Eventure</b> — {today}"]
    # One message per source family; the header says which one it is.
    if section:
        header[0] += f" · {_esc(section)}"
    footer = _footer(health, cfg)
    budget = 3900 - len("\n".join(header + [footer]))

    shown: list[tuple[Event, str]] = []
    body: list[str] = []

    if not items and not signals:
        body.extend(["", L["quiet"]])

    if items:
        body.extend(["", f"<b>{L['events']}</b>"])
        used = 0
        for event, short in items[: cfg.max_events]:
            block = _event_block(event, cfg)
            cost = len("\n".join(block))
            if used + cost > budget:
                break
            body.extend(block)
            shown.append((event, short))
            used += cost
        hidden = len(items) - len(shown)
        if hidden:
            body.extend(["", f"<i>{_esc(L['more'].format(n=hidden))}</i>"])

    if signals:
        body.extend(["", f"<b>{L['signals']}</b>"])
        for signal in signals[:6]:
            # The handle is more informative than the feed's own name.
            origin = f"@{signal.channel}" if signal.channel else signal.source
            body.append(f"• {_esc(signal.title[:110])} ({_esc(origin)})")
            if signal.url:
                body.append(f"  {_esc(signal.url)}")

    keyboard = [
        [
            {
                "text": f"➕ {dates.format_when(event.start, cfg.language, event.all_day)} · {event.title}"[:58],
                "callback_data": f"a:{short}",
            }
        ]
        for event, short in shown
    ]
    return "\n".join(header + body + [footer])[:4096], keyboard


def render_card(event: Event, short: str, cfg: Config) -> tuple[str, list[list[dict]]]:
    """Caption for a photo card. Telegram allows 1024 characters here."""
    L = labels(cfg)
    lines = [f"<b>{_esc(event.title)}</b>"]
    meta = dates.format_when(event.start, cfg.language, event.all_day)
    if event.channel:
        meta += f" · @{_esc(event.channel)}"
    if event.location:
        meta += f" · {_esc(_shorten(event.location, 60))}"
    lines.append(meta)
    if event.confidence != "confirmed":
        lines.append(f"<i>{L['candidate']}</i>")
    if event.url:
        lines.append(_esc(event.url))
    keyboard = [[{"text": f"➕ {L['add']}", "callback_data": f"a:{short}"}]]
    return "\n".join(lines)[:1024], keyboard


def render_footer_only(health: list[SourceHealth], cfg: Config) -> str:
    """When every event became its own card there is no list left to send."""
    return _footer(health, cfg)


def render_broken(health: list[SourceHealth], cfg: Config) -> str:
    L = labels(cfg)
    lines = [L["broken"], "", L["broken_hint"]]
    for item in health:
        lines.append(f"• {_esc(item.name)}: {_esc(item.error or 'failed')}")
    return "\n".join(lines)[:4000]
