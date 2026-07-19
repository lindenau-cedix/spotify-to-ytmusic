# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local-first tool that migrates Spotify playlists to YouTube Music. It runs entirely on the user's machine — there's no cloud deploy, no telemetry, and the "web UI" is just FastAPI on `127.0.0.1`. One user, one machine.

The full functional spec lives in the original task prompt the repo was built from; see `docs/architecture.md` for the matching strategy and SQLite schema, and `README.md` for the auth walkthroughs and troubleshooting.

## Common commands

```bash
make install          # editable install of src/migrator + runtime deps
make install-dev      # adds pytest + pytest-asyncio + respx
make test             # full pytest run
make lint             # compileall only — there is no ruff/flake8 config
make serve            # web UI on http://127.0.0.1:8000
make run-auth         # walks through both auth flows
make auth-status      # python -m migrator auth status (same idea as `status`)

# A single test, or a node:
.venv/bin/python -m pytest tests/test_scoring.py -v
.venv/bin/python -m pytest tests/test_scoring.py::test_scoring_table -v
.venv/bin/python -m pytest -k perfect_match

# End-to-end CLI usage:
.venv/bin/python -m migrator list
.venv/bin/python -m migrator run <playlist_id> --dry-run
.venv/bin/python -m migrator status          # bare `python -m migrator` is the same
.venv/bin/python -m migrator auth status    # spotify/ytm auth rows only

# The pipeline one stage at a time. Every stage takes an optional playlist id
# and applies to *all* exported playlists when you omit it, so test on one id
# first. `run` chains export+match+import; use the stages when you want to
# review between matching and importing, which is the point of the review UI.
.venv/bin/python -m migrator export <playlist_id>          # Spotify -> playlists+tracks
.venv/bin/python -m migrator match  <playlist_id>          # -> matches (--lenient to skip
                                                           #    unfindable tracks instead of
                                                           #    failing the playlist on a throttle)
.venv/bin/python -m migrator accept <playlist_id>          # flip review -> accepted
.venv/bin/python -m migrator import <playlist_id> --yes    # push accepted matches to YTM

make run PLAYLIST=<playlist_id>   # the Makefile's `run` passes PLAYLIST through

# Convert a Chrome DevTools "Copy as cURL (bash)" string into the YTM
# headers JSON file. Only needed for the non-cookie headers now (see
# browser_cookies.py) or when running with [ytm] cookie_source = "file":
./scripts/curl_to_ytm_headers.py --clip     # clipboard (Wayland/X11/macOS)
pbpaste | ./scripts/curl_to_ytm_headers.py  # stdin

# Is the session actually authenticated? YouTube answers an unauthenticated
# request with a normal HTTP 200 and anonymous content, so this is the only
# quick way to tell:
.venv/bin/python -c "from migrator.ytm_client import ensure_client; \
  print(ensure_client().logged_in_flag())"   # "1" = signed in, "0" = not
```

The venv at `.venv/` is the install location this session used (the system pip at `/opt/venv/` is root-owned and unwritable). If `make install` fails on permissions, create a venv: `python3 -m venv .venv && .venv/bin/pip install -e .`

## Pinned-version gotcha

`typer==0.15.1` is incompatible with `click>=8.2` — `Parameter.make_metavar() missing 'ctx'` on `--help`. Both `pyproject.toml` and `requirements.txt` pin `click>=8.0,<8.2`. If you bump typer, re-test `--help` immediately.

`ytmusicapi` is pinned to `>=1.12.0` — versions before 1.12.0 hit `TypeError: RefreshingToken.__init__() got an unexpected keyword argument 'refresh_token_expires_in'` during the OAuth device flow because Google's token endpoint started returning that field. Don't downgrade without a fix for that.

## Architecture cheat sheet

Five SQLite tables drive everything (see `docs/architecture.md` for the full schema):

- **`playlists`** — one row per Spotify playlist; carries `ytm_playlist_id` once imported, so re-runs resume against the existing YTM playlist.
- **`tracks`** — `(playlist_id, snapshot_id, position)` identity; replaced wholesale on re-export so snapshots stay idempotent.
- **`matches`** — one row per `(playlist_id, snapshot_id, spotify_track_id)`. Holds the `status` (`accepted`/`review`/`skipped`/`manual_*`), the score, and the candidate list. This is the table the review UI writes to.
- **`imported_tracks`** — every videoId actually added to a YTM playlist. The importer subtracts this set before re-adding.
- **`auth_state`** — boolean + detail string per service.

Key data flow: **`export` populates `playlists`+`tracks` → `match` populates `matches` → review UI (or the `accept` command) mutates `matches.status` → `import` reads `matches`+`imported_tracks` to push the deltas to YTM.**

`accept` is the CLI counterpart to the review UI: `db.accept_reviews()` flips `review` → `accepted` for the current snapshot, skipping rows with no `chosen_video_id` (nothing to import). The web `/review/{id}/bulk-accept` endpoint predates it and defaults to a 0.85 threshold; the CLI defaults to accepting everything.

## Module map

- `cli.py` — Typer app. Top-level invocation (no subcommand) prints the status dashboard via `invoke_without_command=True`. `import` is registered with `name="import"` because the bare name is a Python keyword.
- `web.py` — FastAPI. Routes call the same logic the CLI uses; `/playlists/{id}/import` calls `cli.import_cmd` directly rather than duplicating.
- `matching.py` — `score_candidate` is pure and table-tested. `Matcher` does the 3-tier flow. **Important:** ISRC hits get a `+0.15` confidence bonus before classification — without it, real-world paraphrased titles (e.g. "Bohemian Rhapsody - Remastered 2011") fall into `review` despite being the right recording. The bonus is capped at 1.0 and can't float an unrelated result past the threshold because we still require title/artist/duration signal from scoring.
- `spotify_client.py` — Spotipy wrapper. `KeyringCacheHandler` stores the refresh token via `secrets.py`. The local callback server in `_await_local_redirect` only listens for one request, so it's safe to leave the redirect URI bound to `127.0.0.1`.
- `ytm_client.py` — `YTMClient` wraps `ytmusicapi.YTMusic`. The class is designed so `FakeYTMClient` in `tests/conftest.py` can subclass it and skip the parent `__init__` (no real headers needed for tests). Three layers keep a long import alive, in order of importance: **pacing** (`batch_interval_seconds` between `add_playlist_items` batches — YouTube throttles on the *rate* of playlist edits, so this prevents the failures the other two only recover from), **session freshness** (`build_headers` / `_maybe_refresh_browser_cookie`, see `browser_cookies.py`), and **retry** (`_call_with_retry` on `_IMPORT_RETRY`, which covers dropped connections and HTTP 409 as well as 429, plus a one-shot visitor-id refresh for the limiter's spurious 401).

  Measured on a real 52-playlist / ~4000-track library: at `batch_interval_seconds = 2.5` YouTube throttled 12 of ~15 batches and the run died partway through; at `6.0` the same library completed in 9 minutes with **zero** backoffs. Each avoided throttle saves a 30s+ penalty, so slower pacing is also dramatically *faster* — don't "optimise" this value down without re-measuring.
- `browser_cookies.py` — reads the live YTM cookie out of the browser's own store (`browser_cookie3`), because that is the only copy that stays current. `build_headers` swaps **only** the cookie into `headers_auth.json`'s other headers: `x-goog-authuser` / `x-goog-pageid` must survive, since they select the account when several are signed in. `YTMClient._maybe_refresh_browser_cookie` re-reads every `[ytm] refresh_interval_seconds` *during* a run and immediately on an auth failure — a single read at startup would still go stale mid-import. `[ytm] cookie_source` is `auto` (live, falling back to the file), `browser` (live only, fail loudly), or `file` (original snapshot behaviour). On Linux the store is encrypted with a key from the login keyring, so this needs an unlocked keyring session.
- `config.py` — hand-rolled TOML parser in `_parse_toml_text` because we don't want to pin `tomllib` availability across Python versions. Settings are `@lru_cache`d; call `config.reset_cache()` after mutating `.env` or `config.toml` in tests.
- `secrets.py` — keyring primary, Fernet-encrypted file fallback. The fallback key lives at `.migrator/fallback.key` with 0600 perms; the encrypted blob is at `.migrator/fallback.bin`. `_real_keyring_available()` does a probe write/delete to detect a working backend.

## Testing

The matching engine is the only piece with non-trivial logic; everything else is glue. Test coverage reflects that:

- `tests/test_scoring.py` — 8 parametrized scenarios pinning every weight in the scoring function, including the partial-title-token credit (max +0.15) that's not in the original spec.
- `tests/test_matching_end_to_end.py` — replays the 3 fixtures (`perfect_match.json`, `fuzzy_match.json`, `no_match.json`) through a `FakeYTMClient` to verify the full pipeline classifies correctly.
- `tests/test_cli_smoke.py` / `tests/test_cli_run_wipe.py` — CLI surface and the destructive `run --wipe` path.
- `tests/test_cli_accept.py` — `db.accept_reviews()` (snapshot scoping, threshold, the no-candidate guard, dry-run) plus the `accept` CLI wrapper.
- `tests/test_ytm_client.py` / `tests/test_spotify_client.py` — header parsing, retry behaviour, and OAuth wiring without going to the network.
- `tests/test_cli_import_dedupe.py` — the two duplicate defences in `_import_one` (order-preserving dedupe of `todo`, reconciliation against the live YTM playlist plus the `imported_tracks` backfill). `FakeYTMClient.live_video_ids` scripts what YouTube reports as already present; `FakeYTMClient.sent_video_ids` captures the batch actually sent.
- `tests/test_secrets_and_db.py` — keyring + Fernet fallback paths plus idempotent DB upserts.
- `tests/conftest.py::isolated_settings` — redirects `MIGRATOR_DATA_DIR` / `MIGRATOR_DB_PATH` into `tmp_path` and calls `config.reset_cache()`. **Use this fixture in any test that touches the DB or the keyring.** `FakeYTMClient` lives in `conftest.py` too; it subclasses `YTMClient` and skips the parent `__init__`, so it needs no real YTM headers.
- `tests/conftest.py::_no_browser_cookies` — **autouse**, stubs out both `_browser_cookie_or_none` and `YTMClient._maybe_refresh_browser_cookie`. Without it every test would decrypt the developer's actual Chrome profile (slow, keyring-dependent, absent on CI). The second stub matters for a subtler reason: the refresh sits on every request path and calls `time.monotonic()`, which would silently consume the scripted clocks the pacing tests iterate over. To exercise the real refresh, capture it at **module import time** — `_REAL_COOKIE_REFRESH = YTMClient._maybe_refresh_browser_cookie` in `test_ytm_client.py` — and call it explicitly; by fixture time the attribute is already replaced.
- Anything building a `YTMClient` via `YTMClient.__new__` (as `_client_with_ytm` does) must set `_last_create_monotonic`, `_last_headers_write` and `_last_cookie_read`, or the pacing/refresh guards raise `AttributeError`.

When adding a new matching scenario: drop a JSON fixture under `tests/fixtures/`, then add a one-shot test in `test_matching_end_to_end.py` that loads it and runs `Matcher.match_one`. Don't write a new scoring branch without also extending `test_scoring.py`.

## Things that will bite you if you don't know

- **`fake_ytm_client` and `Matcher.match_many` use `asyncio.to_thread()`**, so the matching tests must be async-friendly. `pytest-asyncio` is configured in `mode = "auto"` in `pyproject.toml` — any `async def test_…` works.
- **The review UI's `/playlists/{id}/import` endpoint calls `cli.import_cmd`**, which uses `typer.confirm` and raises `typer.Abort()` on no. That means HTTP 400 on rejection. Don't try to make it interactive from the browser — it's wrapped with `--yes`.
- **`run_auth_flow` for Spotify opens a browser and listens on `SPOTIFY_REDIRECT_URI`'s port**. If you're running tests that touch the auth flow, redirect the port to something free, or skip entirely — the `KeyringCacheHandler.get_cached_token` path will return `None` and the flow will trigger, which is not what tests want.
- **ytmusicapi parses the response body as JSON *before* checking the HTTP status** (`_send_request`). When YouTube throttles with an HTML/empty error page, you get a bare `JSONDecodeError("Expecting value…")`, not a 429. `_call_with_retry` treats any `JSONDecodeError` as a throttle (`_is_throttle`) and wraps exhaustion in `YTMNonJSONResponseError` with a rate-limit hint. On exhaustion `add_playlist_items` stops the *whole playlist* rather than hammering the limiter with the remaining batches, and sets `last_add_aborted` so the caller reports "re-run to resume" instead of a clean success — a short return list alone can't distinguish that from "YTM deduped most of them".
- **"Session signed out" means the exported headers went *stale*, not that the cookie expired or the account logged out.** A `headers_auth.json` captured from DevTools stops authenticating ~15–20 minutes later while the browser it came from stays signed in. Google rotates the session-continuation cookies (`__Secure-1PSIDTS` / `__Secure-3PSIDTS`, the `SIDCC` family) through `accounts.google.com`; the browser follows that rotation, a frozen snapshot cannot. Confirmed with a control — at the same instant, same account, the snapshot's cookie returned `"LOGGED_IN":false` from `music.youtube.com` and the browser's live cookie returned `"LOGGED_IN":true`. The fix is `browser_cookies.py`; see below.
- **Do not try to fix session staleness by reading rotated cookies off our own responses.** This looks like the obvious answer and it does not work: the `youtubei` endpoints never send the continuation cookies back. Measured across a full 52-playlist import, the *only* cookies YouTube ever rotated to us were `VISITOR_PRIVACY_METADATA`, `YSC` and `__Secure-ROLLOUT_TOKEN` — none of which authenticate anything. `YTMClient._sync_rotated_cookies` does adopt what the jar offers, but it is defence-in-depth, not the mechanism that keeps a run alive. It refuses to adopt `_IDENTITY_COOKIES` (`SID`, `SAPISID`, `__Secure-*PSID`, …) precisely because a throttled reply can carry an *anonymous* cookie set, and persisting that would overwrite real credentials on disk — turning a transient throttle into a lockout only a fresh export could undo.
- **ytmusicapi picks its auth type solely from the `authorization` header** (`determine_auth_type`), and two of three outcomes fail silently. `SAPISIDHASH …` → `BROWSER`, the only mode that re-signs each request from the cookie's `__Secure-3PAPISID` — a stale timestamp in the captured value is harmless. `Bearer …` → `OAUTH_CUSTOM_FULL`, sent verbatim until it expires. **Absent** → `OAUTH_CUSTOM_CLIENT`, which rebuilds `base_headers` from `initialize_headers()` and *drops the cookie entirely*, making every request anonymous. `load_headers` rejects the latter two via `_assert_browser_auth`, because both surface much later as a "signed out" error that blames a perfectly valid cookie.
- **`Imported 0/N tracks` was a hard failure, not dedupe.** This note used to claim it was benign dedupe. It was not: **YouTube's playlist edit is atomic on duplicates.** If any videoId in a batch is repeated *within the batch*, or is already in the playlist, YouTube answers `STATUS_FAILED` and adds **nothing** — including the tracks that were fine. Measured directly against the live API: sending `[C, C, D]` to an empty playlist added neither C nor D. At `batch_size = 50`, one collision therefore costs 50 good tracks. Confirmed on the real library: three playlists whose YTM playlists were *empty* reported `Imported 0/43`, `0/34`, `0/18` — nothing existed to dedupe against, the duplicates were inside the batch. Across the library this was silently losing ~1286 tracks per run.

  Two sources fed it, both now guarded in `_import_one`: (1) `todo` was a plain list, so a matcher mapping two Spotify tracks onto one video (album + single releases, or a dupe in the Spotify playlist) repeated an id — now an order-preserving dedupe; (2) `imported_tracks` records only *confirmed* adds, so a run killed mid-playlist left tracks live on YTM we never recorded and re-sent forever — now reconciled against `YTMClient.playlist_video_ids()` (the live `get_playlist` read) and backfilled. `playlist_video_ids` returns an empty set when the read fails, so the recorded set stays authoritative rather than the playlist looking empty and everything being re-sent.

  **A third source can't be prevented, only isolated: YouTube canonicalises videoIds on add.** Send `dQw4w9WgXcQ` and `get_playlist` afterwards reports `lYBUbBu4W08` — measured. So two distinct ids the matcher chose can resolve to the same underlying track, and the collision is invisible to us: it isn't a repeat in `todo`, and it isn't in the live playlist read either. (The tell is `imported_tracks` rows whose videoId never appears in `get_playlist` — 12 of 357 on one real playlist.) Only YouTube knows, and it tells us by refusing. `add_playlist_items` therefore **bisects a refused batch** down to a single id, so one poisoned track costs one track instead of 50, and reports the offender in `rejected_video_ids`. Isolated ids are deliberately **not** recorded as imported: the refusal usually means "already present under the canonical id", but it can also mean "video unavailable", and a phantom row would permanently skip a track that never landed. Re-isolating it on the next run is the cheaper mistake.

  `_confirmed_added` still refuses to record what the response didn't confirm — recording phantoms would subtract them from every future run and permanently skip tracks that never landed. `_batch_rejected` now separates the atomic refusal from genuine dedupe, and `add_playlist_items` sets `last_add_rejected` so the CLI reports it in red instead of a warning that read like success. **Don't add `duplicates=True` to `add_playlist_items` as a shortcut** — it makes the batch land, but YouTube then stores the duplicate, so playlists accumulate repeated tracks.
- **The fallback secrets file is a single JSON dict, encrypted**. If `fallback.bin` exists but `fallback.key` was lost, all secrets in it are unrecoverable; the loader logs a warning and starts empty rather than erroring.
- **No migrations.** `SQLModel.metadata.create_all` runs on first `get_engine()` call. Adding a column to a table means either dropping the DB or writing a manual `ALTER TABLE` — there's no Alembic config.