# Connect Google Calendar — step by step

**Why this happens on your laptop and not on the Orange Pi.** Google only accepts
returning the permission to `http://127.0.0.1:8765`, i.e. to the same machine where
you are viewing the browser. The Pi has no browser and is not reachable from the
internet, so it cannot receive that response. The solution: authorise once on the
laptop, then copy the resulting file (`token.json`) to the Pi. That file holds a
long-lived *refresh token*; the worker uses it to request short-lived tokens when it
needs to write an event.

Time: about 10 minutes. Cost: 0.

---

## 1. Create the client in Personal mode

1. Open <https://console.cloud.google.com/auth/overview/personal>
2. Sign in with **the same Google account whose calendar you want to use**.
3. If it offers to create a project, accept. Suggested name: `eventure`.
4. Application type: **Desktop app**.
5. Accept the terms and press **Continue**.
6. A dialog appears with the *Client ID* and *Client secret*. Press **Download JSON**
   and keep it. **The secret is shown only once.**

What "Personal mode" means: a single-account project that **requires no Google
verification and never shows the "unverified app" screen**. It cannot be converted to
another mode later, but you do not need to.

Leave *Use this client for an AI-powered agent* unchecked: it is a declaration that
the client is used by AI agents/MCP, and it grants no permissions. Eventure calls the
API directly.

## 2. Enable the Calendar API

1. Open <https://console.cloud.google.com/apis/library/calendar-json.googleapis.com>
2. Check at the top that the selected project is `eventure`.
3. Press **Enable**. (If the wizard already did it, you will see *Manage*.)

Skipping this step produces `403 ... has not been used in project` on the first event.

## 3. Put the file in the project

In the project folder, next to `google_auth.py`:

```sh
mv ~/Downloads/client_secret_*.json ./client_secret.json
```

## 4. Authorise

```sh
python3 google_auth.py
```

What you will see:

1. The browser opens on Google's consent screen.
2. You pick your account.
3. "Eventure wants access to your Google Account" → permission
   **"Create or modify events"** → **Allow**.
4. The page says "Done. You may close this tab."
5. In the terminal: `wrote token.json`.

If the browser does not open, run it with `--no-browser`, copy the long URL it prints
and paste it manually.

Exact scope requested: `https://www.googleapis.com/auth/calendar.events`. It creates
and modifies events. It cannot read your mail, see your contacts or delete calendars.

## 5. Move the token to the right place

```sh
mkdir -p secrets
mv token.json secrets/token.json
```

In Docker, `./secrets` is mounted inside the container as `/app/secrets`, read-only,
and `config.toml` already points there.

## 6. Check that it works

```sh
python3 -m eventure run --dry-run     # builds the digest, does not touch the calendar
python3 -m eventure poll              # leave it running and press a ➕ in Telegram
```

The bot should reply "added to your calendar ✓" (in your configured language) and the
event should appear in your primary calendar.

---

## Common problems

| Symptom | What it means | Fix |
| --- | --- | --- |
| `redirect_uri_mismatch` | The OAuth client is not type *Desktop app* | Create another client of that type |
| `no refresh_token returned` | New consent was not requested | Re-run `google_auth.py` (it uses `prompt=consent`) |
| `invalid_grant` when pressing ➕ | Token revoked or invalid | Delete `secrets/token.json` and repeat steps 4–5 |
| `token.json is a directory` | Docker created a folder because the file was missing | Delete the folder and put the real file there |
| `403 insufficientPermissions` | `calendar_id` does not belong to that account | Use `primary` or the right calendar ID |
| `CERTIFICATE_VERIFY_FAILED` | Python without a CA bundle | Handled in `eventure/http.py`; if it persists you are on an older copy |
| `409 Conflict` in logs | Two processes polling `getUpdates` | Stop one of them |

## What you do NOT need

- Do not publish the app or go through verification: Personal mode does not ask for it.
- Do not commit `token.json` or `client_secret.json`: both are in `.gitignore`.
- Do not run the flow on the Pi or inside the container.

## If you ever revoke access

Google → <https://myaccount.google.com/permissions> → Eventure → Remove access.
The worker stops being able to add events (the bot replies with an error) and the rest
of the system keeps working. To reconnect, repeat from step 4.
