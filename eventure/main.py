"""CLI.

    python -m eventure run --dry-run     # build the digest, print it, send nothing
    python -m eventure run               # send the digest
    python -m eventure serve             # scheduler + button handler (this is the container)
    python -m eventure poll              # only the button handler
    python -m eventure ics out.ics       # write a calendar file (no credentials)
    python -m eventure status            # source health from state
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import collect, digest, gcal, ics, store
from .config import Config, ConfigError, load
from .model import Event, Signal
from .telegram import Telegram, TelegramError


def _event_from_record(uid: str, record: dict) -> Event:
    start = datetime.fromisoformat(record["start"])
    end = datetime.fromisoformat(record["end"]) if record.get("end") else None
    return Event(
        uid=uid,
        title=record["title"],
        start=start,
        end=end,
        location=record.get("location", ""),
        url=record.get("url", ""),
        image=record.get("image", ""),
        source=record.get("source", ""),
        channel=record.get("channel", ""),
        all_day=bool(record.get("all_day")),
        confidence=record.get("confidence", "confirmed"),
    )


def _new_signals(state: store.Store, signals: list[Signal], now: datetime) -> list[Signal]:
    seen = state.data.setdefault("signals", {})
    fresh = [signal for signal in signals if signal.uid not in seen]
    for signal in fresh:
        seen[signal.uid] = now.isoformat()
    return fresh


def seconds_until_next(times: tuple[str, ...], tz, now: datetime | None = None) -> float:
    """Seconds until the next HH:MM in the configured timezone."""
    now = now or datetime.now(tz)
    targets = []
    for raw in times:
        try:
            hour, minute = (int(part) for part in str(raw).split(":", 1))
        except (ValueError, TypeError):
            continue
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        targets.append(target)
    if not targets:
        targets = [now + timedelta(days=1)]
    return max(1.0, min((target - now).total_seconds() for target in targets))


def _section_of(source_name: str, cfg: Config) -> str:
    """Web feed or social account? The digest sends them as separate messages."""
    for source in cfg.sources:
        if source.name == source_name:
            return "instagram" if source.type.startswith("instagram") else "web"
    return "web"


def _apply_digest(
    cfg: Config, state: store.Store, gathered
) -> tuple[list[tuple[str, list[list[dict]], list[tuple[Event, str]]]], int]:
    """Turn a collection result into one digest per source family.

    Mutates and returns the state payload. A group with nothing new and no
    failing source is dropped, so a quiet Instagram day does not cost you an
    extra message.
    """
    now = datetime.now(cfg.tz)
    state.ingest(gathered.events, now)
    signals = _new_signals(state, gathered.signals, now)

    # Everything still unnotified, not just this run's fresh events: --resend
    # clears notified_at and expects those events to come back. A stale pending
    # event from an old failed run is dropped instead of resurrected.
    shown: list[Event] = []
    pairs: list[tuple[Event, str]] = []
    for uid, record in state.data["events"].items():
        if record.get("notified_at"):
            continue
        # A skipped event stays skipped even after --resend; only the notified
        # flag is cleared by a reset.
        if record.get("skipped_at"):
            continue
        event = _event_from_record(uid, record)
        if event.start < now - timedelta(days=1):
            # A past event restored by --resend can never be attended; retire it
            # instead of reporting it as pending forever.
            state.mark_skipped(uid, now)
            continue
        shown.append(event)
        pairs.append((event, record["short"]))
    pairs.sort(key=lambda pair: pair[0].start)

    all_failed = bool(gathered.health) and not any(item.ok for item in gathered.health)
    sections: list[tuple[str, list[list[dict]], list[tuple[Event, str]]]] = []

    if all_failed:
        sections.append((digest.render_broken(gathered.health, cfg), [], []))
    else:
        buckets = {
            key: {"pairs": [], "signals": [], "health": []} for key in ("web", "instagram")
        }
        for event, short in pairs:
            buckets[_section_of(event.source, cfg)]["pairs"].append((event, short))
        for signal in signals:
            buckets[_section_of(signal.source, cfg)]["signals"].append(signal)
        for item in gathered.health:
            buckets[_section_of(item.name, cfg)]["health"].append(item)

        for key, bucket in buckets.items():
            group_pairs, sigs, health = bucket["pairs"], bucket["signals"], bucket["health"]
            failing = any(not item.ok for item in health)
            if not group_pairs and not sigs and not failing:
                continue
            cards = [pair for pair in group_pairs if pair[0].image]
            rest = [pair for pair in group_pairs if not pair[0].image]
            if rest or sigs or not cards:
                text, keyboard = digest.render(
                    rest, sigs, health, cfg, section=digest.labels(cfg).get(key, key)
                )
            else:
                # Every event became its own card; the message still carries health.
                text, keyboard = digest.render_footer_only(health, cfg), []
            sections.append((text, keyboard, cards))

        if not sections:
            sections.append((digest.render([], [], gathered.health, cfg), [], []))

    state.record_health(gathered.health)
    state.prune()
    if not all_failed:
        state.mark_notified(shown, now)
    return sections, len(shown)


def _send_digest(
    cfg: Config, sections: list[tuple[str, list[list[dict]], list[tuple[Event, str]]]]
) -> int:
    bot = Telegram(cfg.telegram_token)
    sent = 0
    for text, keyboard, cards in sections:
        for event, short in cards:
            caption, card_keyboard = digest.render_card(event, short, cfg)
            try:
                bot.send_photo(cfg.telegram_chat_id, event.image, caption, card_keyboard)
            except TelegramError as exc:
                # A dead or hotlink-protected image must not lose the event.
                print(f"photo card failed ({exc}); sending it as text", file=sys.stderr)
                bot.send(cfg.telegram_chat_id, caption, card_keyboard)
            sent += 1
            time.sleep(1.0)  # stay under Telegram's per-chat flood control
        bot.send(cfg.telegram_chat_id, text, keyboard)
    return sent


def cmd_run(cfg: Config, args) -> int:
    gathered = collect.collect(cfg)
    state = store.Store.load(cfg.state_path)

    if getattr(args, "resend", False):
        events_reset, signals_reset = state.reset_notified(
            include_added=getattr(args, "include_added", False)
        )
        print(
            f"resend: cleared {events_reset} event(s) and {signals_reset} signal(s)",
            file=sys.stderr,
        )

    sections, fresh = _apply_digest(cfg, state, gathered)
    cards = sum(len(section[2]) for section in sections)

    print(
        f"collected {len(gathered.events)} events / {len(gathered.signals)} signals "
        f"from {len(gathered.health)} sources; {fresh} new, {cards} with image, "
        f"{len(sections)} message(s)",
        file=sys.stderr,
    )

    if args.dry_run:
        for text, keyboard, section_cards in sections:
            for event, _short in section_cards:
                print(f"[card] {event.title}  ->  {event.image}")
            print(text)
            if keyboard:
                print(
                    f"\n[{len(keyboard)} botones: "
                    f"{', '.join(row[0]['text'] for row in keyboard[:5])}…]"
                )
            print("\n" + "─" * 40)
        return 0

    if not cfg.telegram_token or not cfg.telegram_chat_id:
        print("telegram token/chat_id missing — nothing sent (use --dry-run)", file=sys.stderr)
        return 2

    sent_cards = _send_digest(cfg, sections)
    state.save()
    print(
        f"sent {len(sections)} message(s) to chat {cfg.telegram_chat_id} "
        f"({sent_cards} card(s))",
        file=sys.stderr,
    )
    return 0


def poll_forever(cfg: Config, offset: int | None, lock: threading.Lock | None = None) -> int:
    """Handle the Add buttons until interrupted."""
    token = None
    if cfg.google_token_path:
        try:
            token = gcal.load_token(cfg.google_token_path)
        except gcal.GoogleError as exc:
            # A broken calendar must not take the bot down; buttons explain it.
            print(f"calendar disabled: {exc}", file=sys.stderr)

    bot = Telegram(cfg.telegram_token)
    guard = lock or threading.Lock()
    labels = digest.labels(cfg)
    print("polling for button presses (Ctrl-C to stop)", file=sys.stderr)

    for update in bot.poll(offset):
        if "message" in update:
            message = update["message"]
            chat = message.get("chat", {})
            command = (message.get("text") or "").strip().split("@")[0]
            # Only answer in the configured chat; ignore groups the bot was added to.
            if chat.get("id") == cfg.telegram_chat_id and command in ("/start", "/help"):
                bot.send(chat["id"], digest.labels(cfg)["help"])
            continue

        callback = update.get("callback_query")
        if not callback:
            continue
        data = str(callback.get("data", ""))
        callback_id = callback["id"]
        if not data.startswith("a:"):
            bot.answer(callback_id, "?")
            continue

        with guard:
            state = store.Store.load(cfg.state_path)
            found = state.by_short(data[2:])
            uid, record = (found if found else (None, None))
            already = bool(record and record.get("added_at"))

        if not uid:
            bot.answer(callback_id, labels["not_found"])
            continue
        if already:
            bot.answer(callback_id, labels["already_added"])
            continue
        if token is None:
            bot.answer(callback_id, labels["no_calendar"])
            continue

        try:
            created = gcal.insert_event(
                token, cfg.google_calendar, _event_from_record(uid, record), cfg.timezone
            )
        except gcal.GoogleError as exc:
            print(f"calendar error: {exc}", file=sys.stderr)
            bot.answer(callback_id, labels["add_failed"])
            continue

        with guard:
            state = store.Store.load(cfg.state_path)
            state.mark_added(uid, created, datetime.now(cfg.tz))
            state.save()
        bot.answer(callback_id, labels["added"])
        print(f"added {record['title']!r} as {created}", file=sys.stderr)
    return 0


def cmd_poll(cfg: Config, args) -> int:
    if not cfg.telegram_token:
        print("telegram token missing", file=sys.stderr)
        return 2
    return poll_forever(cfg, args.offset)


def cmd_serve(cfg: Config, args) -> int:
    """One process, two jobs: scheduled digests and the button handler.

    This is what runs in Docker. Two threads, one state file, one lock.
    """
    lock = threading.Lock()
    stop = threading.Event()

    def run_digest_once() -> None:
        try:
            gathered = collect.collect(cfg)
            with lock:
                state = store.Store.load(cfg.state_path)
                sections, fresh = _apply_digest(cfg, state, gathered)
                state.save()
            if cfg.telegram_token and cfg.telegram_chat_id:
                cards = _send_digest(cfg, sections)
                print(
                    f"digest sent ({fresh} new, {cards} card(s), "
                    f"{len(sections)} message(s))",
                    file=sys.stderr,
                )
            else:
                print("telegram not configured; digest built but not sent", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - a bad run must not kill the service
            print(f"digest run failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    def digest_loop() -> None:
        if cfg.run_on_start:
            run_digest_once()
        while not stop.is_set():
            wait = seconds_until_next(cfg.digest_times, cfg.tz)
            print(f"next digest in {int(wait)}s", file=sys.stderr)
            if stop.wait(wait):
                return
            run_digest_once()

    if not cfg.telegram_token:
        print("telegram token missing", file=sys.stderr)
        return 2

    threading.Thread(target=digest_loop, name="digest", daemon=True).start()
    try:
        return poll_forever(cfg, args.offset, lock)
    finally:
        stop.set()


def cmd_ics(cfg: Config, args) -> int:
    gathered = collect.collect(cfg)
    if not gathered.events:
        print("no upcoming events collected", file=sys.stderr)
        return 1
    target = Path(args.path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(ics.build(gathered.events, cfg.timezone, "Eventure"), encoding="utf-8")
    print(f"wrote {len(gathered.events)} events to {target}")
    return 0


def cmd_sources_import(cfg: Config, args) -> int:
    """Turn the human source list into `[[source]]` blocks in config.toml."""
    from . import sources as sources_module

    added, skipped, unknown = sources_module.import_sources(
        Path(args.config),
        Path(args.sources_file),
        probe=args.probe,
        dry_run=args.dry_run,
        timeout=args.timeout,
    )

    for name, url, note in unknown:
        print(f"  skipped  {name}: {note}", file=sys.stderr)
    for name, url, existing in skipped:
        print(f"  already  {url} (as {existing!r})")

    if not added:
        print("nothing to add: config.toml already covers every readable source")
        return 0

    print("-- would add --" if args.dry_run else "-- added --")
    for name, url in added:
        print(f"  {name}: {url}")
    if args.dry_run:
        print("\n(dry run: config.toml untouched)")
    else:
        print(f"\nwrote {len(added)} source(s) to {args.config}")
    return 0


def cmd_status(cfg: Config, args) -> int:
    state = store.Store.load(cfg.state_path)
    ok, total, failing = state.health_summary()
    print(f"sources: {ok}/{total} healthy")
    for name, record in state.data["sources"].items():
        flag = "FAIL" if record.get("consecutive_failures", 0) else "ok  "
        checked = str(record.get("checked_at", "never"))[:19]
        print(
            f"  {flag} {name}: checked={checked} items={record.get('last_items', 0)} "
            f"baseline={record.get('baseline_items', 0)} "
            f"fails={record.get('consecutive_failures', 0)} {record.get('last_error', '')[:80]}"
        )
    pending = len(state.pending())
    print(f"pending (never notified): {pending}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eventure")
    parser.add_argument("--config", default="config.toml", help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="collect and send one digest")
    run.add_argument("--dry-run", action="store_true", help="print instead of sending")
    run.add_argument(
        "--resend",
        action="store_true",
        help="repeat events already notified (keeps added/skipped ones quiet)",
    )
    run.add_argument(
        "--include-added",
        action="store_true",
        help="with --resend, replay events already in the calendar too",
    )
    run.set_defaults(func=cmd_run)

    poll = sub.add_parser("poll", help="long-poll Telegram for button presses")
    poll.add_argument("--offset", type=int, default=None)
    poll.set_defaults(func=cmd_poll)

    serve = sub.add_parser("serve", help="digest scheduler + button handler in one process")
    serve.add_argument("--offset", type=int, default=None)
    serve.set_defaults(func=cmd_serve)

    export = sub.add_parser("ics", help="write an .ics file")
    export.add_argument("path", nargs="?", default="eventure.ics")
    export.set_defaults(func=cmd_ics)

    status = sub.add_parser("status", help="show source health")
    status.set_defaults(func=cmd_status)

    sources_parser = sub.add_parser(
        "sources", help="keep config.toml in sync with your sources.txt"
    )
    sources_sub = sources_parser.add_subparsers(dest="sources_command", required=True)
    sources_import = sources_sub.add_parser(
        "import", help="append missing [[source]] blocks to config.toml"
    )
    sources_import.add_argument(
        "--from", dest="sources_file", default="sources.txt", help="list file (default sources.txt)"
    )
    sources_import.add_argument("--dry-run", action="store_true", help="show what would be added")
    sources_import.add_argument(
        "--probe", action="store_true", help="try common feed paths on plain HTML pages"
    )
    sources_import.add_argument("--timeout", type=float, default=20.0, help="per-request timeout")
    sources_import.set_defaults(func=cmd_sources_import)

    args = parser.parse_args(argv)
    try:
        cfg = load(args.config)
        return args.func(cfg, args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except TelegramError as exc:
        print(f"telegram error: {exc}", file=sys.stderr)
        return 3
    except gcal.GoogleError as exc:
        print(f"google error: {exc}", file=sys.stderr)
        return 4
