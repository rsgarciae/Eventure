"""Import a plain source list into `config.toml`.

Two files describe sources, and they served different purposes:

* `sources.txt`  — the human list you keep, read by the Phase 0 audit.
* `config.toml`  — what the worker actually polls, with an explicit type per source.

Keeping both in sync by hand rots: you add a venue to one and forget the other,
and the digest quietly never shows it. This module turns the first into the
second, and refuses to write anything it cannot read back:

    python -m eventure sources import --dry-run    # show the blocks, touch nothing
    python -m eventure sources import              # append the missing [[source]] blocks

A URL only becomes `ics` or `rss` when the audit can actually read a feed from it.
Instagram profiles become `instagram_profile`, other social hosts are reported and
left out, and anything unreadable is named instead of silently dropped.
"""

from __future__ import annotations

import os
import sys
import time
import tomllib
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .config import ConfigError

SOCIAL_SKIP = ("facebook", "fb", "tiktok", "x", "twitter", "threads")


def key(url: str) -> str:
    """Normalised identity of a source, for spotting duplicates.

    Drops the fragment and the host case, and throws away the query string for
    social profiles — `instagram.com/venue?knvt=...` is the same profile as
    `instagram.com/venue`. A query elsewhere is kept, because `?ical=1` is the
    whole point of that URL.
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/") or "/"
    social = any(host == domain or host.endswith("." + domain) for domain in
                 ("instagram.com", "facebook.com", "fb.com", "threads.net", "tiktok.com", "x.com"))
    query = "" if social else parts.query
    return f"{host}{path}" + (f"?{query}" if query else "")


def page_key(url: str) -> str:
    """Host + path, ignoring query and fragment: "is this the same page?".

    `site/events/` and `site/events/?ical=1` are one page with two renditions: if
    the config already holds either, the other adds nothing. This also stops the
    importer from picking the site-wide blog feed off a page whose events feed you
    already poll — a strictly worse source.
    """
    parts = urlsplit(url.strip())
    return f"{(parts.hostname or '').lower()}{parts.path.rstrip('/') or '/'}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def render(name: str, kind: str, url: str = "", path: str = "") -> str:
    """One `[[source]]` block, as TOML text."""
    lines = ["[[source]]", f'name = "{_escape(name)}"', f'type = "{_escape(kind)}"']
    if url:
        lines.append(f'url = "{_escape(url)}"')
    if path:
        lines.append(f'path = "{_escape(path)}"')
    return "\n".join(lines)


def merge(
    config_text: str, entries: list[tuple[str, str, str, str]]
) -> tuple[str, list[tuple[str, str]], list[tuple[str, str, str]]]:
    """Append the missing sources to `config_text`.

    ``entries`` are ``(name, type, url, path)``. Returns
    ``(new_text, added, skipped)`` where `skipped` is ``(name, url, existing name)``.
    Pure on purpose: no network, no writes, easy to test.
    """
    data = tomllib.loads(config_text)
    existing: dict[str, str] = {}
    for source in data.get("source", []):
        if source.get("url"):
            existing[key(str(source["url"]))] = str(source.get("name", ""))
    has_feed_reader = any(source.get("type") == "instagram_json" for source in data.get("source", []))

    blocks: list[str] = []
    added: list[tuple[str, str]] = []
    skipped: list[tuple[str, str, str]] = []
    needs_feed_reader = False

    for name, kind, url, path in entries:
        identity = key(url) if url else f"{kind}:{path}"
        if identity in existing:
            skipped.append((name, url or path, existing[identity]))
            continue
        blocks.append(render(name, kind, url=url, path=path))
        added.append((name, url or path))
        existing[identity] = name
        if kind == "instagram_profile":
            needs_feed_reader = True

    if needs_feed_reader and not has_feed_reader:
        blocks.append(render("Instagram feed", "instagram_json", path="data/instagram.json"))
        added.append(("Instagram feed", "data/instagram.json"))

    if not blocks:
        return config_text, added, skipped

    new_text = config_text.rstrip("\n") + "\n\n" + "\n\n".join(blocks) + "\n"
    try:
        if len(tomllib.loads(new_text).get("source", [])) < len(data.get("source", [])):
            raise ConfigError("the merged config lost sources; refusing to write it")
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"refusing to write invalid TOML: {exc}") from exc
    return new_text, added, skipped


def parse_list(path: Path) -> list[tuple[str, str]]:
    """Read a `sources.txt`: one per line, optional `Name | URL`."""
    if not path.exists():
        raise ConfigError(
            f"sources file not found: {path}\n"
            "Your watch list is private and is not shipped with the repo. Create it:\n"
            "    cp sources.example.txt sources.txt"
        )
    pairs: list[tuple[str, str]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, url = (part.strip() for part in line.split("|", 1)) if "|" in line else (line, line)
        if not url.lower().startswith(("http://", "https://")):
            print(f"{path}:{number}: skipping non-URL line", file=sys.stderr)
            continue
        pairs.append((name or url, url))
    return pairs


def _audit_module():
    try:
        import audit  # ships next to the package, at the repo root
    except ImportError as exc:  # pragma: no cover - only in odd layouts
        raise ConfigError(
            "audit.py is not importable; run this from the repository root "
            "(or the container, which copies it)"
        ) from exc
    return audit


def detect(url: str, probe: bool = False, timeout: float = 20.0) -> tuple[str | None, str, str]:
    """Work out which source type this URL is. Returns (type, url to store, note)."""
    audit = _audit_module()

    platform = audit.social_platform(url)
    if platform == "instagram":
        return "instagram_profile", url, "collected by ig_collect.py"
    if platform:
        return None, url, f"{platform}: third-party accounts expose no feed"

    status, content_type, body, error = audit.fetch(url, timeout)
    if error:
        return None, url, f"unreachable: {error}"

    kind = audit.classify(content_type, body)
    if kind in ("ics", "rss"):
        return kind, url, "served directly"

    text = audit.decode(body, content_type)
    for _kind, href in audit.discover_feeds(text, url)[:2]:
        sub_kind, _events, sub_error = audit.read_feed(href, timeout)
        if sub_kind:
            return sub_kind, href, "advertised by the page"
        if sub_error:
            continue

    if probe:
        for path in audit.PROBE_PATHS:
            href = urljoin(url, path)
            sub_kind, events, _ = audit.read_feed(href, timeout)
            if sub_kind and events:
                return sub_kind, href, "found by probing"
            time.sleep(0.5)

    return None, url, "no ICS/RSS found" + ("" if probe else "; retry with --probe")


def import_sources(
    config_path: Path,
    list_path: Path,
    probe: bool = False,
    dry_run: bool = False,
    timeout: float = 20.0,
) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """Detect every source in `list_path` and append the missing ones to config.

    Returns ``(added, skipped, unknown)``.
    """
    text = Path(config_path).read_text(encoding="utf-8")
    entries: list[tuple[str, str, str, str]] = []
    skipped: list[tuple[str, str, str]] = []
    unknown: list[tuple[str, str, str]] = []

    covered = {
        page_key(str(source["url"])): str(source.get("name", ""))
        for source in tomllib.loads(text).get("source", [])
        if source.get("url")
    }

    for name, url in parse_list(Path(list_path)):
        page = page_key(url)
        if page in covered:
            # Already watched under another rendition; skip before any request.
            skipped.append((name, url, covered[page]))
            continue
        kind, store_url, note = detect(url, probe=probe, timeout=timeout)
        if kind is None:
            unknown.append((name, url, note))
            continue
        entries.append((name, kind, store_url, ""))

    new_text, added, merged_skipped = merge(text, entries)
    skipped.extend(merged_skipped)
    if not dry_run and added:
        target = Path(config_path)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, target)
    return added, skipped, unknown
