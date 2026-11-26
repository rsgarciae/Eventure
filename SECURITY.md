# Security Policy

Eventure runs unattended on a small machine at home, holds a Telegram bot token, a
Google OAuth refresh token and an Instagram session, and reads untrusted text from
the public internet. Security reports are welcome and taken seriously.

## Supported versions

Only the current `main` branch and the most recent tagged release receive fixes.
There are no long-term support branches: the project is small enough that a fix
lands as a commit and you re-pull.

| Version | Supported |
|---------|-----------|
| `main` (latest commit) | :white_check_mark: |
| latest tagged release  | :white_check_mark: |
| anything older         | :x: |

## Reporting a vulnerability

**Please do not open a public issue for a vulnerability.**

1. Preferred: report it privately through GitHub, which keeps the details hidden
   until a fix is ready:
   <https://github.com/rsgarciae/Eventure/security/advisories/new>
   The same form is reachable from the **Security** tab of the repository, under
   **Report a vulnerability**.
2. If that form is unavailable, open a regular issue that says only *"I have a
   security report, please contact me privately"* and no details, so a private
   channel can be arranged.

The repository's own policy page, if you prefer to read this as published:
<https://github.com/rsgarciae/Eventure/security/policy>

Useful in a report: affected component (`eventure/` module, `ig_collect.py`,
Dockerfile, systemd units), the version or commit, and what an attacker gains.

Please allow a reasonable window to respond before public disclosure. Reports are
acknowledged as fast as the maintainer can, and fixes are credited unless you ask
otherwise.

## Scope

In scope:

- Code execution, injection or path traversal reachable from untrusted input — an
  ICS/RSS feed, an Instagram caption, or a Telegram callback.
- Leakage of `TELEGRAM_BOT_TOKEN`, `secrets/token.json`, `data/ig_session.json`,
  `client_secret*.json` or `.env` — for example through logs, error messages printed
  to the digest, or the state file.
- Authentication and authorisation flaws: a second Telegram chat being able to press
  the ➕ buttons or read the digest.
- Flaws in the shipped Docker image or in the systemd units under `deploy/`.
- Dependency vulnerabilities that are actually reachable from this code.

Out of scope:

- Findings that require an already-compromised host or root on the machine.
- Rate limiting, account checkpoints or blocks imposed by Telegram, Google or
  Instagram. Instagram in particular is scraped with an unofficial client by
  design; account-side consequences are documented in `docs/design.md`.
- Missing hardening headers or similar on services the project does not serve —
  there is no inbound network listener at all, only Telegram long polling.

## Handling secrets

If you are deploying this yourself:

- `.env`, `secrets/`, `data/` and `client_secret*.json` are git-ignored on purpose;
  keep them that way.
- If a token may have leaked, revoke it — do not just rotate it quietly. For the bot
  token that means `/revoke` in @BotFather and a new token; for Google, delete the
  OAuth client or revoke the refresh token in your account.
- `data/ig_session.json` is a live login for the Instagram account. Treat it as a
  password, and delete it after an account scare.
