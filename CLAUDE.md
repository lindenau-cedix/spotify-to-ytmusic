# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local-first tool that migrates Spotify playlists to YouTube Music. It runs entirely on the user's machine — there's no cloud deploy, no telemetry, and the "web UI" is just FastAPI on `127.0.0.1`. One user, one machine.

The full functional spec lives in the original task prompt the repo was built from; see `docs/architecture.md` for the matching strategy and SQLite schema, and `README.md` for the auth walkthroughs and troubleshooting.

## Common commands

```bash
make install          # editable install of src/migrator + runtime deps
make test             # full pytest run
make serve            # web UI on http://127.0.0.1:8000
make run-auth         # walks through both auth flows

# A single test:
.venv/bin/python -m pytest tests/test_scoring.py -v
.venv/bin/python -m pytest tests/test_scoring.py::test_scoring_table -v
.venv/bin/python -m pytest -k perfect_match

# End-to-end CLI usage:
.venv/bin/python -m migrator list
.venv/bin/python -m migrator run <playlist_id> --dry-run
.venv/bin/python -m migrator status          # bare `python -m migrator` is the same
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

Key data flow: **`export` populates `playlists`+`tracks` → `match` populates `matches` → review UI mutates `matches.status` → `import` reads `matches`+`imported_tracks` to push the deltas to YTM.**

## Module map

- `cli.py` — Typer app. Top-level invocation (no subcommand) prints the status dashboard via `invoke_without_command=True`. `import` is registered with `name="import"` because the bare name is a Python keyword.
- `web.py` — FastAPI. Routes call the same logic the CLI uses; `/playlists/{id}/import` calls `cli.import_cmd` directly rather than duplicating.
- `matching.py` — `score_candidate` is pure and table-tested. `Matcher` does the 3-tier flow. **Important:** ISRC hits get a `+0.15` confidence bonus before classification — without it, real-world paraphrased titles (e.g. "Bohemian Rhapsody - Remastered 2011") fall into `review` despite being the right recording. The bonus is capped at 1.0 and can't float an unrelated result past the threshold because we still require title/artist/duration signal from scoring.
- `spotify_client.py` — Spotipy wrapper. `KeyringCacheHandler` stores the refresh token via `secrets.py`. The local callback server in `_await_local_redirect` only listens for one request, so it's safe to leave the redirect URI bound to `127.0.0.1`.
- `ytm_client.py` — `YTMClient` wraps `ytmusicapi.YTMusic`. The class is designed so `FakeYTMClient` in `tests/conftest.py` can subclass it and skip the parent `__init__` (no real headers needed for tests).
- `config.py` — hand-rolled TOML parser in `_parse_toml_text` because we don't want to pin `tomllib` availability across Python versions. Settings are `@lru_cache`d; call `config.reset_cache()` after mutating `.env` or `config.toml` in tests.
- `secrets.py` — keyring primary, Fernet-encrypted file fallback. The fallback key lives at `.migrator/fallback.key` with 0600 perms; the encrypted blob is at `.migrator/fallback.bin`. `_real_keyring_available()` does a probe write/delete to detect a working backend.

## Testing

The matching engine is the only piece with non-trivial logic; everything else is glue. Test coverage reflects that:

- `tests/test_scoring.py` — 8 parametrized scenarios pinning every weight in the scoring function, including the partial-title-token credit (max +0.15) that's not in the original spec.
- `tests/test_matching_end_to_end.py` — replays the 3 fixtures (`perfect_match.json`, `fuzzy_match.json`, `no_match.json`) through a `FakeYTMClient` to verify the full pipeline classifies correctly.
- `tests/conftest.py::isolated_settings` — redirects `MIGRATOR_DATA_DIR` / `MIGRATOR_DB_PATH` into `tmp_path` and calls `config.reset_cache()`. **Use this fixture in any test that touches the DB or the keyring.**

When adding a new matching scenario: drop a JSON fixture under `tests/fixtures/`, then add a one-shot test in `test_matching_end_to_end.py` that loads it and runs `Matcher.match_one`. Don't write a new scoring branch without also extending `test_scoring.py`.

## Things that will bite you if you don't know

- **`fake_ytm_client` and `Matcher.match_many` use `asyncio.to_thread()`**, so the matching tests must be async-friendly. `pytest-asyncio` is configured in `mode = "auto"` in `pyproject.toml` — any `async def test_…` works.
- **The review UI's `/playlists/{id}/import` endpoint calls `cli.import_cmd`**, which uses `typer.confirm` and raises `typer.Abort()` on no. That means HTTP 400 on rejection. Don't try to make it interactive from the browser — it's wrapped with `--yes`.
- **`run_auth_flow` for Spotify opens a browser and listens on `SPOTIFY_REDIRECT_URI`'s port**. If you're running tests that touch the auth flow, redirect the port to something free, or skip entirely — the `KeyringCacheHandler.get_cached_token` path will return `None` and the flow will trigger, which is not what tests want.
- **The fallback secrets file is a single JSON dict, encrypted**. If `fallback.bin` exists but `fallback.key` was lost, all secrets in it are unrecoverable; the loader logs a warning and starts empty rather than erroring.
- **No migrations.** `SQLModel.metadata.create_all` runs on first `get_engine()` call. Adding a column to a table means either dropping the DB or writing a manual `ALTER TABLE` — there's no Alembic config.