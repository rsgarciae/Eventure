"""Configuration: one TOML file, read at run time, never baked into code.

Location is optional. If latitude/longitude are absent, radius filtering is
simply not applied — the worker still runs. That keeps "where I am" a value you
set once, not an assumption the system makes.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SOURCE_TYPES = ("ics", "rss", "instagram_profile", "instagram_json")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Source:
    name: str
    type: str
    url: str = ""
    path: str = ""
    geo_filter: bool = False


@dataclass(frozen=True)
class Config:
    language: str = "en"
    timezone: str = "UTC"
    date_order: str = "DMY"
    horizon_days: int = 60
    past_grace_hours: int = 2
    max_events: int = 20
    digest_times: tuple[str, ...] = ("08:00",)
    run_on_start: bool = False
    state_path: Path = Path("state.json")
    telegram_token: str = ""
    telegram_chat_id: int = 0
    google_token_path: Path | None = None
    google_calendar: str = "primary"
    city: str = ""
    lat: float | None = None
    lon: float | None = None
    radius_km: float | None = None
    sources: tuple[Source, ...] = ()

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"unknown timezone {self.timezone!r}") from exc

    @property
    def geo_enabled(self) -> bool:
        return self.lat is not None and self.lon is not None and self.radius_km is not None


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env support. Never overrides variables already set."""
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def _source(raw: dict) -> Source:
    kind = str(raw.get("type", "")).strip()
    if kind not in SOURCE_TYPES:
        raise ConfigError(f"source {raw.get('name')!r}: unknown type {kind!r} (use {', '.join(SOURCE_TYPES)})")
    name = str(raw.get("name") or raw.get("url") or raw.get("path") or "").strip()
    if not name:
        raise ConfigError("source without name/url/path")
    return Source(
        name=name,
        type=kind,
        url=str(raw.get("url", "")).strip(),
        path=str(raw.get("path", "")).strip(),
        geo_filter=bool(raw.get("geo_filter", False)),
    )


def load(path: str | Path = "config.toml") -> Config:
    _load_dotenv()
    file = Path(path)
    if not file.exists():
        raise ConfigError(f"{file} not found — copy config.example.toml and edit it")
    with file.open("rb") as handle:
        raw = tomllib.load(handle)

    general = raw.get("general", {})
    location = raw.get("location", {})
    telegram = raw.get("telegram", {})
    google = raw.get("google", {})
    schedule = raw.get("schedule", {})

    token = os.environ.get(str(telegram.get("token_env", "TELEGRAM_BOT_TOKEN")), "")
    if not token:
        token = str(telegram.get("token", ""))

    sources = tuple(_source(item) for item in raw.get("source", []))
    if not sources:
        raise ConfigError("no [[source]] entries in config")

    return Config(
        language=str(general.get("language", "en")).lower(),
        timezone=str(general.get("timezone", "UTC")),
        date_order=str(general.get("date_order", "DMY")).upper(),
        horizon_days=int(general.get("horizon_days", 60)),
        past_grace_hours=int(general.get("past_grace_hours", 2)),
        max_events=int(general.get("max_events", 20)),
        digest_times=tuple(str(item) for item in schedule.get("times", ["08:00"])),
        run_on_start=bool(schedule.get("run_on_start", False)),
        state_path=Path(general.get("state_path", "state.json")),
        telegram_token=token,
        telegram_chat_id=int(telegram.get("chat_id", 0) or 0),
        google_token_path=Path(google["token_path"]) if google.get("token_path") else None,
        google_calendar=str(google.get("calendar_id", "primary")),
        city=str(location.get("city", "")),
        lat=float(location["lat"]) if "lat" in location else None,
        lon=float(location["lon"]) if "lon" in location else None,
        radius_km=float(location["radius_km"]) if "radius_km" in location else None,
        sources=sources,
    )
