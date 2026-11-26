# Eventure — design record

Status: Phase 1 built and verified (ICS source, Instagram collector, Google
Calendar write, Docker deployment).

Goal: replace a manual weekly sweep of ~20 sources with a single Telegram digest of
real upcoming events, where each event is added to the calendar with one tap.

## Two problems, deliberately separated

- **A. Watch sources I already know.** Bounded and mostly feed-shaped. Solvable now.
- **B. Discover unknown events city-wide.** Unbounded; every option degrades to
  scraping or a paid aggregator. Out of scope until A works and is still insufficient.

Anything that claims to solve B cheaply is solving A with extra steps.

## Location is configuration, never a constant

`config.toml` holds `city`, `lat`, `lon`, `radius_km`, `language` and `timezone`.
Every component reads them. No city, country, region, language or currency is baked
into code, selectors or documentation. Radius filtering applies only to
platform-wide feeds, never to sources the user lists explicitly.

## Verified facts that shaped the design

| Claim | Status | Source |
| --- | --- | --- |
| Eventbrite's public event-search API was shut down 2019-12-12; only org/venue-scoped listing remains | verified | [Eventbrite API](https://www.eventbrite.com/platform/new/api) |
| Meetup uses a GraphQL API at `api.meetup.com/gql-ext`, OAuth2 bearer, 500 points/60 s | verified | [Meetup API guide](https://www.meetup.com/api/guide/) |
| Google removed the OOB (`urn:ietf:wg:oauth:2.0:oob`) redirect; loopback `http://127.0.0.1:port` is the desktop redirect | verified | [OAuth for desktop apps](https://developers.google.com/identity/protocols/oauth2/native-app) |
| Google "Personal mode" projects are single-account, need no verification, show no unverified-app warning and have no publishing status | verified | [Get started with Google Auth Platform](https://support.google.com/cloud/answer/15544987) |
| External + Testing projects issue refresh tokens expiring in 7 days and cap at 100 new users | verified | [Refresh token expiration](https://developers.google.com/identity/protocols/oauth2#expiration), [App audience](https://support.google.com/cloud/answer/15549945) |
| changedetection.io supports arm64 / ARM Pis, Telegram via apprise, and can emit RSS from changes | verified | [changedetection.io](https://github.com/dgtlmoon/changedetection.io) |
| Instaloader supports profiles, stories, hashtags and session reuse; stories need login | verified | [Instaloader](https://github.com/instaloader/instaloader) |
| snscrape is abandoned (last commit ~3 years ago); do not build on it | verified | [snscrape](https://github.com/JustAnotherArchivist/snscrape) |
| Huginn is actively maintained but requires MySQL/PostgreSQL | verified | [Huginn](https://github.com/huginn/huginn) |
| Luma calendars offer an ICS subscribe link | **unverified** | check your own account |
| Meetup groups still expose an iCal export | **unverified** | check the current site |
| Facebook/Instagram official APIs cannot read third-party accounts or search city-wide | high confidence, not re-verified today | Meta Graph API docs |

## Chosen approach: feeds first, scraping never (until the audit says otherwise)

Reuse what already exists. Build only the last mile, because no off-the-shelf tool
does *normalise → dedupe → digest → approve → write calendar*.

Phases, each useful on its own:

1. **Phase 0 — feed audit.** `audit.py` classifies each source: ICS, RSS/Atom,
   advertises a feed, or nothing. Decision gate: ≥75 % machine-readable → feeds
   first; ≤50 % → scraping becomes the critical path.
2. **Phase 1 — one small worker.** Reads ICS/RSS/JSON. Normalises to one event
   record, dedupes, drops past events, sends one Telegram digest and writes to
   Google Calendar on a button press. *Deliverable: coverage of known sources
   without social credentials.*
3. **Phase 2 — RSS-Bridge** (one container) for venue pages with no feed. Still no
   authentication.
4. **Phase 3 — social, isolated.** A disposable container emitting a JSON feed of new
   posts; the worker consumes it like any other source. If Meta breaks it, only
   social coverage is lost.
5. **Phase 4 — discovery, optional.** Ticketing APIs (Ticketmaster where the region
   is covered; Meetup groups you join; Eventbrite venues you follow). Partial by
   design.

## Worker shape

Source adapters → normalise → dedupe → filter (future, radius, language) → digest →
Telegram long polling → calendar writer.

- State: **one** JSON file, rewritten once per run (SD-card safe). Pruned at 120
  days. No database server.
- Dedup key: source-native UID, plus a fuzzy
  `sha1(title + start date + venue)` hash to merge the same event published by a
  venue and by a ticketing platform.
- Past filter: dropped when the event ends; ongoing events kept; default horizon 60
  days.
- No inbound ports: Telegram `getUpdates` only.
- One process (`eventure serve`) with two threads: digest scheduler and button
  handler, sharing the state file behind a lock.
- No Chrome/Playwright on the Pi. No local inference, no vector store, no queue.

## Failure signalling: "no events" must never mean "collection broke"

Per source we persist `last_success`, HTTP status, consecutive failures and item
count.

- Digest footer: `Sources: 17 OK / 1 empty / 2 failing`, naming the failing ones.
- Zero items **and** zero successful fetches → send "collection failed", not a
  digest.
- Item count far below baseline → the source is flagged suspicious (catches silent
  markup changes in selector bridges).
- Each adapter runs isolated with its own timeout, so one failure never aborts the
  whole run.

## Calendar handoff

1. **`.ics` document** — zero credentials, works with any calendar app. Produced by
   `eventure ics out.ics`. Tradeoff: it is a snapshot; later changes at the source do
   not propagate, and re-importing may duplicate unless the UID matches.
2. **Google Calendar API** (the default in this installation) — scope
   `https://www.googleapis.com/auth/calendar.events`, `events.insert` on `primary`.
   Headless consent on the Pi: **Personal mode** project + **Desktop** client +
   loopback, completed on a laptop, then `token.json` copied over. Personal mode
   avoids both the unverified-app screen and the 7-day expiry that External/Testing
   imposes.
3. **CalDAV** — Apple/Fastmail/Nextcloud/mailbox.org with an app-specific password;
   would be created with a `PUT` of a single-`VEVENT` `VCALENDAR` to
   `<collection>/<uid>.ics` with `If-None-Match: *`. Designed but **not implemented**.

In this installation the default is **Google Calendar**; `.ics` always exists as a
credential-free fallback, and CalDAV stays documented as an alternative. All three
share one key: the event UID.

## Known risks

- **Instagram, 2026-11-22 — blocked.** Login succeeds (fresh session, 2FA accepted)
  and `instagram.com` returns `200` for the profile page and login page from this
  network, so the account is not locked and the IP is not blocked wholesale. What is
  refused is the data endpoint: every profile answers
  `400 ... "feedback_required"` on `graphql/query`, with a fresh session, with a
  browser user-agent, and after a security prompt was cleared in the app. The
  authenticated page HTML carries no post JSON (it is a JS shell), so there is no
  scrape-without-JavaScript fallback. Not resolvable in code; revisit after a
  cool-down. The retry loop is stopped so it stops adding requests.
- **Instagram, 2026-11-26 — still blocked, and Instaloader has no working route.**
  Re-tested after four days. `graphql/query` still answers `feedback_required`;
  `/api/v1/users/web_profile_info/` returns `404` on `www.instagram.com` **and** on
  `i.instagram.com` (mobile app id). Instaloader 4.16 is the current release, so this
  is a client/API mismatch as much as a block: the endpoints it knows are gone or
  refused.
- **Instagram, TLS impersonation ruled out.** The last untested hypothesis was that
  Instagram fingerprints the TLS handshake, which is why plain Python clients get
  throttled while browsers do not. Tested directly against the real endpoint with
  `curl_cffi` impersonating Chrome, reusing the session's 11 cookies and Instaloader's
  own headers: `POST /graphql/query -> 400 {"message":"feedback_required","spam":true}`.
  So the refusal is account/IP-level on that endpoint, not a signature a client can
  imitate. Two incidental findings: swapping `context._session` accomplishes nothing
  because `graphql_query()` calls `_graphql_session()` and passes a *temporary*
  session explicitly, and `curl_cffi`'s `Session.cookies` cannot be reassigned
  (the property setter discards it), so a wrapper presenting a `requests`-shaped
  `.cookies` view is required if this is ever revisited.
- **Instagram — resolution: switched to `instagrapi`, and it works.** The private
  mobile API is a different endpoint family from the web GraphQL one being refused,
  so it was the only free route left. Verified 2026-11-26: login with 2FA accepted,
  then **11 posts from 3 profiles, 0 errors**, of which 4 became dated events and 5
  stayed signals. That confirms the block was specific to `graphql/query`, not the
  account or the IP. Cost is a one-time password login and a small account-flagging
  risk; `instaloader` stays in the code as a non-default engine for reference.
- **Time extraction dropped `pm`.** The caption "arrancamos en punto de las 9:00 pm"
  produced a 09:00 event, because the clock regex read `9:00` and never looked at the
  marker after it. Fixed in `dates._clock()` for `am`/`pm`, `a. m.`/`p. m.` and the
  Spanish day parts (`de la mañana/tarde/noche/madrugada`). A bare number is still
  never read as a time, so "28 de noviembre" stays all-day. 5 tests added.
- **Weekday dates were invisible.** "Este jueves 26 se canta con todo" carries no
  month, so it was filed as "no date found" and an event happening that same day was
  never shown. `dates.find()` now accepts a weekday next to a day number, but only
  after checking that the day really falls on that weekday in one of the next three
  months; a mismatch returns nothing rather than a guess.
- **Instagram images were dropped.** The collector never recorded an image URL, so
  every Instagram event arrived as text. `ig_collect.py` now stores the first image
  (carousels expose `resources`, and `thumbnail_url` is a pydantic `HttpUrl` that must
  be stringified before it can be serialised) and `collect.py` passes it to the
  `Event`, which turns it into a photo card. Links are signed with `oe=` and last a
  few days; a dead link falls back to the text event by design.
- **Replaying state hid later corrections.** Fixing the image only produced 2 cards
  out of 6 possible, because `--resend` replays records stored *before* the fix and an
  event first seen without an image stayed image-less forever. `Store.ingest()` now
  refreshes an existing record from the current collection (title, start, end,
  location, url, image, all_day, confidence) while keeping its short id and the
  user's `notified_at`/`added_at`/`skipped_at` flags. Tested that a button created
  before the correction still resolves.
- **One message per source family.** Web feeds and Instagram were mixed in a single
  digest, which put caption guesses next to structured feed data. `_apply_digest()`
  now buckets events, signals and health by `_section_of()` and returns a list of
  messages; a family with nothing new and no failing source is skipped instead of
  sent empty. `run --resend` re-sends what was already notified (skipping added and
  skipped events) for exactly this kind of validation; `--include-added` replays the
  added ones too, and their button then answers "already added" rather than writing a
  duplicate into the calendar.
- **A post with two nights produced one event.** The Blues Festival caption said
  "Nos vemos viernes 27 y sábado 28 de noviembre"; `dates.find()` returned the first
  match, so only Saturday existed and ➕ could only add Saturday. `dates.find_all()`
  now returns every date, and `collect._from_instagram` emits one event per night.
  Two rules keep this honest: a single clock time applies to every date, but several
  clock times across several dates leave them all-day rather than attach the wrong
  hour to the wrong night; and an explicit "28 de noviembre" suppresses the
  weekday-only rule for that day, so a caption with a wrong weekday cannot spawn a
  phantom second date.
- **Uid ownership on multi-date posts.** Giving the original uid to the first date
  rewrote the existing Saturday record into Friday, and the fuzzy duplicate check
  then swallowed Saturday (observed as "0 new" with the event visibly collected).
  Single-date posts keep their uid; multi-date posts suffix every event with its
  date. `Store.ingest()` also frees the old fuzzy key when a record's date moves, so
  a different event on the old date is not mistaken for a duplicate.
- **The digest did not say which account posted.** `source` holds the configured
  source name ("Instagram feed"), which says nothing about the venue. `Event.channel`
  and `Signal.channel` now carry the profile and render as `· @handle` in both the
  text list and the card caption. A fuzzy duplicate now *refreshes* the canonical
  record instead of being skipped, which is how the older Saturday record acquired
  its handle without creating a second record or a second button.

- **Two source lists, one command.** `sources.txt` (human, read by the Phase 0
  audit) and `config.toml` (typed, polled by the worker) drifted apart by hand: a
  venue added to one and forgotten in the other means a digest that silently never
  shows it. `eventure sources import` detects the type of each URL — `ics`/`rss`
  only when a feed actually answers, `instagram_profile` for profiles, nothing for
  Facebook — and appends the missing blocks. It skips a URL whose *page* is already
  watched under another rendition, which is how it avoids adding
  `durango.com.mx/feed/` (the site-wide blog RSS) next to the `?ical=1` events feed
  that the same page advertises. The merged config is re-parsed with `tomllib`
  before it is written atomically, so a quoted name cannot corrupt it.

- Meta sessions expire or hit checkpoints → the social adapter returns 401/403/429;
  it shows up as a failing source, never as a quiet week.
- Instagram stories expire in 24 h; polling them 2–4 times a day multiplies
  rate-limit exposure. They are best-effort, not the primary signal.
- Venues change platforms and their `.ics` starts returning 404; caught by the
  item-count warning.
- APIs change: Meetup rewrote its API in February 2025, Eventbrite shut search down
  in 2019. Assume one breaking change per platform every few years.

## Phase 0 result (2026-11-21)

Run against the first real list: 7 rows → 4 social, 2 web feeds, 1 blocked.

| Source | Result |
| --- | --- |
| `durango.com.mx/eventos/` | WordPress + The Events Calendar. `/eventos/?ical=1` → **30 events, 29 upcoming**, with real dates. `/eventos/feed/` also works (events-only RSS) |
| `vamoseventos.com/durango/` | **Blocked**: HTTP 429, `server: Vercel`, `x-vercel-mitigated: challenge`. A JavaScript challenge, not rate limiting: a plain HTTP client cannot pass |
| 3 Instagram profiles | Social. No feed exists for third-party accounts |
| 1 Facebook link | Social, same reason |

The `durango.com.mx` ICS had two defects found by using it, not by reading docs:

1. It arrives **space-flattened**: one 36 KB line, with spaces where CRLF belongs (a
   minifying cache). Non-conformant, but the data is intact. The parser rebuilds the
   property boundaries and reports it instead of repairing it silently.
2. It contains **all-day events** (`DTSTART;VALUE=DATE`), which are timezone-naive and
   crashed the sort against timezone-aware siblings. Normalised before comparing.

Consequences:

- Phase 1 needs exactly two adapters on the web side: ICS + RSS.
- Phase 3 is required for the 4 social sources; they are most of the list.
- The aggregator is not automatable on this hardware without a browser, so it is
  dropped rather than worked around.
- Two web sources is too small a sample to generalise from; the verdict says so.

## Next step

Fill `sources.txt` with real sources and run
`python3 audit.py sources.txt --probe`. The verdict line decides whether Phase 1 is
enough or Phase 3 is mandatory.
