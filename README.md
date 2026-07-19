# spotify-to-ytmusic

A local-first desktop web app for migrating Spotify playlists to YouTube Music.
Runs on your machine only — no cloud, no telemetry, no account on our side.

## What it does

1. Reads your Spotify playlists (including private / collaborative).
2. Finds each track on YouTube Music using a 3-tier matching strategy.
3. Lets you review and approve any uncertain matches in a local web UI.
4. Pushes the approved matches to a new private YouTube Music playlist.
5. Is idempotent — re-running on the same playlist will not create duplicates.

## Prerequisites

- **Python 3.11+**
- A Spotify account
- A YouTube Music account with a logged-in browser session
- About 10 minutes for first-time setup

## Install

```bash
git clone <this repo>
cd spotify-to-ytmusic
make install            # creates no venv; installs into your active python
# OR, if you prefer a clean venv:
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Configure

```bash
cp .env.example .env
```

Then edit `.env` and fill in:

- `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` from
  <https://developer.spotify.com/dashboard>
  (create an app, set the redirect URI to
  `http://localhost:8000/callback/spotify`).
- Other settings have sensible defaults; see `config.toml` for non-secret tuning.

## Authenticate

```bash
make run-auth
# OR run them individually:
python -m migrator auth spotify   # opens browser, captures refresh token
python -m migrator auth ytm       # paste your YTM headers JSON
```

### YouTube Music header extraction (the annoying part)

YouTube Music's unofficial Python client doesn't use OAuth — it reuses your
browser's authenticated request headers. To get those:

1. Open <https://music.youtube.com> in Chrome or Firefox. **Sign in.**
2. Open DevTools (`F12`) → **Network** tab → check **"Preserve log"**.
3. Click around (Library, a playlist, anything that triggers a request).
4. Right-click any request row → **Copy → Copy as cURL (bash)**.
5. Paste into <https://curlconverter.com/> (or run `ytmusicapi browser` and
   paste the cURL there). You need at minimum the JSON object containing
   `cookie` and `x-goog-authuser`. Save it as
   `.migrator/headers_auth.json`.

`python -m migrator auth ytm` walks you through this and accepts either the
file path or an upload through the web UI at `/setup`.

### Where secrets live

- **Spotify refresh token**: stored encrypted in your OS keyring via
  `keyring`. If no keyring is available (headless server, broken D-Bus),
  falls back to a Fernet-encrypted file under `.migrator/`.
- **YTM headers**: plaintext JSON under `.migrator/headers_auth.json`. This is
  unavoidable — YTM headers are session-bound and not encryptable in a way
  the server can use, so keep the file permissions tight.

## Migrate a playlist

`run` is incremental — it exports your Spotify playlists once, then on
later invocations just pushes the already-decided matches to YouTube Music.

```bash
# See your playlists:
python -m migrator list

# First run: exports Spotify playlists into the DB.
python -m migrator run

# Then match + resolve review rows in the UI:
python -m migrator match
python -m migrator serve              # review UI on http://127.0.0.1:8000

# …or skip the UI and take every review match as-is:
python -m migrator accept

# Subsequent runs: skip the export, push the accepted matches to YTM.
python -m migrator run                # or `python -m migrator import --yes`

# Single-playlist version (same incremental behavior):
python -m migrator run <playlist_id>
```

Or step-by-step if you want full control:

```bash
python -m migrator export <playlist_id>
python -m migrator match  <playlist_id>
python -m migrator serve              # resolve review rows
python -m migrator import <playlist_id>
```

### Accepting review rows without the UI

`match` parks anything it isn't confident about in `review`, and `import`
ignores those rows. `accept` clears that queue in bulk — it's the CLI
equivalent of clicking accept on every row in the review UI:

```bash
python -m migrator accept                      # every playlist
python -m migrator accept <playlist_id>        # just one
python -m migrator accept -t 0.8               # only rows scoring ≥ 0.8
python -m migrator accept --dry-run            # show the counts, write nothing
python -m migrator accept --yes                # no confirmation prompt
```

Review rows that matching found *no* candidate for are left alone — there's no
videoId to import, so accepting them would quietly drop the track. Resolve
those in the UI (or leave them). There's no un-accept command, so `--dry-run`
first if you're unsure; re-running `match` re-scores everything from scratch.

`export`, `match`, `accept`, `import`, and `run` all accept an optional `playlist_id` —
omit it to run the command over every exported playlist:

```bash
# Migrate everything in two stages:
python -m migrator run                # first call: export + import
# (resolve review rows in the web UI)
python -m migrator run                # later calls: just import

# Or step-by-step across all playlists:
python -m migrator export                 # all Spotify playlists → DB
python -m migrator match                  # match every exported playlist
python -m migrator serve                  # resolve review rows in the UI
python -m migrator import --yes           # push every accepted match to YTM
```

In batch mode the per-playlist failures are logged and the loop continues;
the run exits non-zero if any playlist failed, so you can re-run to retry
just the ones that errored out. `import` without `--yes` shows one summary
prompt up front for the whole batch instead of per playlist.

For a 200-track playlist you should expect:

- ~80%+ auto-accepted (no manual work)
- ~10–20% in the review queue, resolvable in a few minutes via the web UI
- ~0–5% with no good YTM match — these are logged and skipped

## Web UI

`python -m migrator serve` starts FastAPI on `127.0.0.1:8000`:

- `/` — dashboard, auth state, playlist list
- `/setup` — first-run wizard
- `/review` — manual review queue with bulk-accept, per-row "Use this", manual
  search, and skip buttons
- `/playlists/{id}/import` — push approved matches to YTM

All review decisions autosave to SQLite; you can close the tab and come back.

## Dry runs

Every mutating command accepts `--dry-run`:

```bash
python -m migrator run <playlist_id> --dry-run
python -m migrator import <playlist_id> --dry-run

# Dry-run the whole library at once:
python -m migrator run --dry-run
python -m migrator import --dry-run
```

Dry runs print what would happen, including the planned YTM playlist name,
without calling any YTM write endpoints.

## Resume

Import is idempotent. If you interrupt a 200-track import at track 87, the
next run sees the existing YTM playlist id in SQLite, re-fetches the match
list, and only adds the un-imported videoIds. Already-imported tracks are
not re-added (YTM dedupes within a playlist, but skipping is cheaper).

## Project layout

```
src/migrator/
  __init__.py
  __main__.py        # python -m migrator
  cli.py             # typer CLI entry point
  config.py          # .env + config.toml loader
  logging_setup.py   # structured JSON logs + stdout
  db.py              # sqlmodel tables + helpers
  secrets.py         # keyring + Fernet fallback
  spotify_client.py  # OAuth + paginated playlist / track fetching
  ytm_client.py      # headers JSON + thin wrapper around ytmusicapi
  matching.py        # 3-tier matching + scoring + persistence
  web.py             # FastAPI app
  templates/         # Jinja2 templates (htmx + Tailwind via CDN)
tests/
  fixtures/          # 3 recorded YTM responses (perfect / fuzzy / no-match)
  test_scoring.py    # table-driven scoring tests
  test_matching_end_to_end.py
  test_secrets_and_db.py
  test_cli_smoke.py
docs/
  architecture.md    # matching strategy + SQLite schema in detail
```

## Troubleshooting

**"SPOTIFY_CLIENT_ID is not set"** — fill it in `.env`.

**Spotify auth loop never receives a callback** — make sure your Spotify app's
redirect URI exactly matches `SPOTIFY_REDIRECT_URI` in `.env`. The default is
`http://localhost:8000/callback/spotify`.

**YTM `KeyError: 'cookie'`** — your `headers_auth.json` is missing the
`cookie` key. Re-extract from DevTools; the cookie alone is usually several KB
long and contains `VISITOR_INFO1_LIVE`, `LOGIN_INFO`, `HSID`, `SSID`, etc.

**YTM "session is signed out" — but my browser is still signed in** — this is
the expected symptom of *stale* headers, not an expired login, and the two are
easy to confuse. Google rotates part of your session cookie
(`__Secure-1PSIDTS` / `__Secure-3PSIDTS`, `SIDCC`) every few minutes and hands
the new values back on each response. Your browser follows that rotation
automatically; the snapshot in `headers_auth.json` cannot, so it ages out while
the browser tab stays happily signed in. The importer now reads those rotated
values off its own responses and writes them back to `headers_auth.json`, so a
long run keeps itself current. If you do hit this, just re-export your headers
— nothing is wrong with your account, and the import resumes where it left off.

**YTM 429 / rate-limited** — all three YTM network calls back off and retry
automatically. Playlist imports (`add_playlist_items`) and playlist creation
(`create_playlist`) use `[importing] rate_limit_backoff_seconds` / `…_max_retries`
(30 s × 5, capped at `rate_limit_max_backoff_seconds`); per-track search uses the
lighter `[matching] search_retry_backoff_seconds` / `…_max_retries`. Retries are
the safety net — the thing that actually *prevents* throttling is
`[importing] batch_interval_seconds`, which paces successive batch adds, since
YouTube limits on the rate of playlist edits. `create_min_interval_seconds` does
the same for playlist creation, which the anti-abuse limiter otherwise rejects
as a spurious `401 "You must be signed in"`. If a batch still comes back
`409 Conflict` after retries, it's split in half and retried down to
`min_batch_size`. If throttling persists, raise `batch_interval_seconds`, lower
`MIGRATOR_MATCH_CONCURRENCY`, or re-run later — reruns resume, never duplicate.

**Spotify 429 during export** — bulk export hits
`/v1/playlists/{id}/items` once per page per playlist and can burst past
Spotify's per-minute budget. The per-page GET retries **indefinitely**
(unattended export would rather wait than orphan half a playlist) — it
honours `Retry-After` (seconds) when Spotify sends one and falls back to
exponential backoff otherwise, with a per-sleep cap so a runaway backoff
never stretches past `rate_limit_max_backoff_seconds`. Knobs live under
`[spotify]` (`rate_limit_backoff_seconds` = 5.0,
`rate_limit_max_backoff_seconds` = 600 by default). Non-429 4xx still
surface immediately as `Skipped … HTTP <code>` so genuine errors don't get
swallowed.

**Region-restricted tracks** — logged and skipped. YTM will silently drop
them from `add_playlist_items`; check the import summary for the
"succeeded N/M" count.

**keyring doesn't work on this machine** — we fall back to a Fernet-encrypted
file under `.migrator/fallback.bin` with 0600 permissions. A warning is
logged the first time this happens. The encryption key is in
`.migrator/fallback.key`.

**Tests fail with `ImportError: typer`** — your click version is too new.
Typer 0.15 requires `click<8.2`. The pinned versions in
`requirements.txt` already enforce this; reinstall with `make install`.

## Development

```bash
make test             # pytest
make lint             # byte-compile only (no formatter pinned)
```

Add new matching scenarios by dropping a JSON fixture under
`tests/fixtures/` and a one-shot test in `test_matching_end_to_end.py`. The
matching scoring function is pure — table-test it directly in
`test_scoring.py` instead of going through the full pipeline.

## License

Personal tool. Do whatever you want with it.