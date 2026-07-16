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

```bash
# See your playlists:
python -m migrator list

# Export + match + import (skips any rows still in review):
python -m migrator run <playlist_id>

# Or step-by-step:
python -m migrator export <playlist_id>
python -m migrator match  <playlist_id>
python -m migrator serve              # review UI on http://127.0.0.1:8000
python -m migrator import <playlist_id>
```

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

**YTM 429 / rate-limited** — the importer backs off 30 s and retries 3×
automatically. If it still fails, slow down by lowering
`MIGRATOR_MATCH_CONCURRENCY` or just re-run `import` later.

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