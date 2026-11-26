#!/usr/bin/env python3
"""One-shot Google consent — run this on a machine with a browser, not on the Pi.

Why: the box has no public callback URL and no browser. The supported flow for a
"Desktop app" OAuth client is a loopback redirect (http://127.0.0.1:PORT), which
only the machine running this script can receive. So consent happens here and the
resulting token.json is copied to the Pi.

Scope is calendar.events only: create/modify events, nothing else.

Setup in Google Cloud Console:
  1. Use Personal mode (single account, no verification, no unverified-app screen):
     https://console.cloud.google.com/auth/overview/personal
  2. Application type: Desktop app
  3. Download the JSON as client_secret.json next to this script
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import secrets
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from eventure.http import urlopen

SCOPE = "https://www.googleapis.com/auth/calendar.events"
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"


def _post_form(fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_URI, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Obtain a Google Calendar refresh token.")
    parser.add_argument("--client", default="client_secret.json")
    parser.add_argument("--out", default="token.json")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    client_file = Path(args.client)
    if not client_file.exists():
        print(f"{client_file} not found — download it from the Cloud console", file=sys.stderr)
        return 2
    raw = json.loads(client_file.read_text(encoding="utf-8"))
    client = raw.get("installed") or raw.get("web") or raw
    client_id = client["client_id"]
    client_secret = client.get("client_secret", "")

    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    redirect_uri = f"http://127.0.0.1:{args.port}"

    auth_url = AUTH_URI + "?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",  # forces a refresh_token even on re-consent
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )

    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in params:
                result["code"] = params["code"][0]
            elif "error" in params:
                result["error"] = params["error"][0]
            body = b"<h2>Eventure</h2><p>Listo. Puedes cerrar esta pestana.</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # silence request logging
            return

    server = http.server.HTTPServer(("127.0.0.1", args.port), Handler)
    server.timeout = 1.0

    print("Open this URL and approve access:", file=sys.stderr)
    print(auth_url, file=sys.stderr)
    if not args.no_browser:
        webbrowser.open(auth_url)

    deadline = time.time() + 600
    while "code" not in result and "error" not in result and time.time() < deadline:
        server.handle_request()
    server.server_close()

    if "error" in result:
        print(f"consent denied: {result['error']}", file=sys.stderr)
        return 1
    if "code" not in result:
        print("timed out waiting for the redirect", file=sys.stderr)
        return 1

    try:
        tokens = _post_form(
            {
                "code": result["code"],
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            }
        )
    except Exception as exc:  # noqa: BLE001 - the code is single-use; explain and stop
        print(f"token exchange failed: {exc}", file=sys.stderr)
        print("Nothing was saved. Re-run and approve again.", file=sys.stderr)
        return 1
    refresh = tokens.get("refresh_token")
    if not refresh:
        print("no refresh_token returned — revoke the app and retry", file=sys.stderr)
        return 1

    token = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh,
        "token_uri": TOKEN_URI,
        "scopes": [SCOPE],
    }
    Path(args.out).write_text(json.dumps(token, indent=2), encoding="utf-8")
    Path(args.out).chmod(0o600)
    print(f"wrote {args.out} — copy it to the Pi and point [google].token_path at it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
