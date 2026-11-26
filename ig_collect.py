#!/usr/bin/env python3
"""Fetch recent Instagram posts into data/instagram.json.

Runs separately from the worker on purpose: when Meta breaks this, the worker
still runs and the digest still renders. Failure stays contained here.

Two engines, both free:

    instagrapi (default)  the private mobile API. Needs a one-time password
                          login, then runs unattended from the stored session:

                              python3 ig_collect.py --engine instagrapi --login

    instaloader           the public web GraphQL, kept for reference. As of
                          2026-11 Instagram answers every graphql/query for
                          these profiles with 400 feedback_required, even with
                          a valid session and Chrome TLS impersonation, so it
                          currently returns nothing.

Then, on a schedule:

    python3 ig_collect.py --days 14 --limit 12

Instagram expires sessions and rate-limits aggressively. A failure here is
expected occasionally, not a bug in this script.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from eventure import config as config_module


ENGINES = ("instagrapi", "instaloader")


def handle_from_url(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return tail.split("?")[0]


def _as_utc(moment: datetime) -> datetime:
    """instagrapi usually returns aware datetimes, but not always."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _post_record(
    shortcode: str, caption: str, taken: datetime, profile: str, image: str = ""
) -> dict:
    return {
        "uid": f"ig:{shortcode}",
        "shortcode": shortcode,
        "caption": caption or "",
        "date_utc": _as_utc(taken).isoformat(),
        "url": f"https://www.instagram.com/p/{shortcode}/",
        "profile": profile,
        "image": image or "",
    }


def _first_image(media) -> str:
    """URL of the first image in a post; carousels carry a list of resources.

    Instagram's CDN links are signed and eventually expire, so a stale link is a
    normal outcome, not a bug: the worker falls back to a text-only event.
    """
    try:
        for resource in getattr(media, "resources", None) or []:
            url = getattr(resource, "thumbnail_url", "") or ""
            if url:
                # instagrapi hands back pydantic HttpUrl objects, not strings.
                return str(url)
        url = getattr(media, "thumbnail_url", "") or ""
        return str(url) if url else ""
    except Exception:  # noqa: BLE001 - an image is a bonus, never a failure
        return ""


def _instagrapi_client(session_path: Path, interactive: bool = False, force_login: bool = False):
    try:
        from instagrapi import Client
        from instagrapi.exceptions import ChallengeRequired, TwoFactorRequired
    except ImportError:
        print("instagrapi is not installed: pip install instagrapi", file=sys.stderr)
        raise SystemExit(2)

    client = Client()
    # Unhurried requests: the mobile API tolerates slow clients far better than
    # it tolerates bursts.
    client.delay_range = [1, 3]

    if session_path.exists() and not force_login:
        client.load_settings(session_path)
        try:
            client.get_timeline_feed()
            return client
        except Exception as exc:  # noqa: BLE001 - stale session, fall back to login
            print(f"session in {session_path} is stale ({type(exc).__name__})", file=sys.stderr)

    user = os.environ.get("INSTAGRAM_USER", "")
    password = os.environ.get("INSTAGRAM_PASSWORD", "")
    if interactive:
        if not user:
            user = input("Instagram username: ").strip()
        if not password:
            password = getpass.getpass("Instagram password: ")
    if not (user and password):
        raise RuntimeError(
            f"no usable session at {session_path}; run: "
            "python3 ig_collect.py --engine instagrapi --login"
        )

    try:
        client.login(user, password)
    except TwoFactorRequired:
        if not interactive:
            raise
        print(
            "Instagram wants a two-factor code (from the app, SMS or authenticator).",
            flush=True,
        )
        code = getpass.getpass("2FA code: ").strip()
        if not code:
            raise RuntimeError("two-factor code required but none was entered")
        client.login(user, password, verification_code=code)
    except ChallengeRequired as exc:
        raise RuntimeError(
            "Instagram sent a challenge instead of a login. Open instagram.com on this "
            "network, approve the prompt ('It was me'), then run --login again."
        ) from exc

    session_path.parent.mkdir(parents=True, exist_ok=True)
    client.dump_settings(session_path)
    return client


def collect_instagrapi(profiles: list[str], args, cutoff: datetime):
    session_path = Path(args.session)
    posts: list[dict] = []
    errors: list[dict] = []

    try:
        client = _instagrapi_client(session_path, interactive=sys.stdin.isatty())
    except Exception as exc:  # noqa: BLE001 - a login failure is a data failure, not a crash
        print(f"login failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return posts, [
            {"profile": name, "error": f"{type(exc).__name__}: {exc}"} for name in profiles
        ]

    for name in profiles:
        try:
            user_id = client.user_id_from_username(name)
            for media in client.user_medias(user_id, amount=args.limit):
                taken = _as_utc(media.taken_at)
                if taken < cutoff:
                    continue
                posts.append(
                    _post_record(
                        media.code, media.caption_text or "", taken, name, _first_image(media)
                    )
                )
        except Exception as exc:  # noqa: BLE001 - one profile must not kill the run
            errors.append({"profile": name, "error": f"{type(exc).__name__}: {exc}"})
            print(f"{name}: {type(exc).__name__}: {exc}", file=sys.stderr)

    return posts, errors


def collect_instaloader(profiles: list[str], args, cutoff: datetime):
    try:
        import instaloader
    except ImportError:
        print("instaloader is not installed: pip install instaloader", file=sys.stderr)
        raise SystemExit(2)

    posts: list[dict] = []
    errors: list[dict] = []

    loader = instaloader.Instaloader(
        quiet=True,
        download_pictures=False,
        download_videos=False,
        download_comments=False,
        save_metadata=False,
        post_metadata_txt_pattern="",
        max_connection_attempts=1,
    )
    if args.session_user:
        try:
            loader.load_session_from_file(args.session_user)
        except Exception as exc:  # noqa: BLE001
            print(
                f"no usable session for {args.session_user!r} ({type(exc).__name__}); "
                f"run: instaloader --login {args.session_user}",
                file=sys.stderr,
            )
            return posts, [
                {"profile": name, "error": f"no session: {type(exc).__name__}"}
                for name in profiles
            ]

    for name in profiles:
        try:
            profile = instaloader.Profile.from_username(loader.context, name)
            taken_count = 0
            for post in profile.get_posts():
                if post.date_utc < cutoff or taken_count >= args.limit:
                    break
                posts.append(
                    _post_record(
                        post.shortcode, post.caption or "", post.date_utc, name,
                        getattr(post, "url", "") or "",
                    )
                )
                taken_count += 1
        except Exception as exc:  # noqa: BLE001 - one profile must not kill the run
            errors.append({"profile": name, "error": f"{type(exc).__name__}: {exc}"})
            print(f"{name}: {type(exc).__name__}: {exc}", file=sys.stderr)

    return posts, errors


COLLECTORS = {
    "instagrapi": collect_instagrapi,
    "instaloader": collect_instaloader,
}


def _login(args) -> int:
    session_path = Path(args.session)
    _instagrapi_client(session_path, interactive=True, force_login=True)
    print(f"session written to {session_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect recent Instagram posts as JSON.")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--out", default="data/instagram.json")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--limit", type=int, default=12, help="posts per profile")
    parser.add_argument(
        "--engine",
        default="instagrapi",
        choices=ENGINES,
        help="instagrapi (mobile API, default) or instaloader (web GraphQL)",
    )
    parser.add_argument(
        "--session",
        default="data/ig_session.json",
        help="instagrapi session file (default: data/ig_session.json)",
    )
    parser.add_argument(
        "--session-user",
        default=os.environ.get("INSTAGRAM_USER", ""),
        help="instaloader session owner (default: $INSTAGRAM_USER)",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="create the instagrapi session file, then exit",
    )
    args = parser.parse_args()

    if args.login:
        return _login(args)

    cfg = config_module.load(args.config)
    profiles = [handle_from_url(s.url) for s in cfg.sources if s.type == "instagram_profile"]
    if not profiles:
        print("no instagram_profile sources in config", file=sys.stderr)
        return 1

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    posts, errors = COLLECTORS[args.engine](profiles, args, cutoff)

    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profiles": profiles,
        "errors": errors,
        "posts": posts,
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        f"wrote {len(posts)} posts from {len(profiles)} profiles to {target} "
        f"({len(errors)} error(s))"
    )
    # Non-zero only when nothing at all worked, so a partial run is still useful.
    return 1 if not posts and errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
