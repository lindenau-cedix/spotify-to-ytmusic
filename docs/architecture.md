# Architecture

## High-level flow

```
Spotify Web API                    YouTube Music (ytmusicapi)
─────────────────                  ──────────────────────────
export_playlists()  ──►  SQLite  ──►  search_songs()
                                 ──►  create_playlist()
                                 ──►  add_playlist_items()

              ▲                                   ▲
              │                                   │
       FastAPI web UI  ◄──── review queue ────────┘
       (Jinja2 + htmx)
```

## SQLite schema

Five tables, all under `.migrator/migrator.db`:

### `playlists`
| column            | type     | notes                                  |
| ----------------- | -------- | -------------------------------------- |
| playlist_id (PK)  | TEXT     | Spotify playlist ID                    |
| snapshot_id       | TEXT     | Spotify snapshot ID for idempotency    |
| name              | TEXT     | displayed in CLI + UI                  |
| description       | TEXT     | carried over to YTM playlist desc      |
| owner             | TEXT     | display name                           |
| track_count       | INTEGER  |                                        |
| is_collaborative  | INTEGER  |                                        |
| is_public         | INTEGER  |                                        |
| spotify_url       | TEXT     |                                        |
| ytm_playlist_id   | TEXT     | populated after first successful import |
| imported_at       | DATETIME |                                        |

Re-exporting with the same `snapshot_id` replaces track rows (idempotent).
A new `snapshot_id` (Spotify mutates the playlist) creates a parallel set of
tracks but keeps the same `playlist_id`, so the matching engine re-runs.

### `tracks`
| column             | type     | notes                              |
| ------------------ | -------- | ---------------------------------- |
| id (PK)            | INTEGER  |                                    |
| playlist_id        | TEXT     | FK → playlists                     |
| snapshot_id        | TEXT     |                                    |
| position           | INTEGER  | 0-based index in the playlist      |
| spotify_track_id   | TEXT     |                                    |
| name               | TEXT     |                                    |
| artists            | TEXT     | JSON list of strings               |
| album              | TEXT     |                                    |
| duration_ms        | INTEGER  |                                    |
| isrc               | TEXT     | used for Tier-1 matching           |
| spotify_url        | TEXT     |                                    |
| raw_json           | TEXT     | full Spotify track payload, JSON   |

### `matches`
| column             | type     | notes                              |
| ------------------ | -------- | ---------------------------------- |
| id (PK)            | INTEGER  |                                    |
| playlist_id        | TEXT     |                                    |
| snapshot_id        | TEXT     |                                    |
| spotify_track_id   | TEXT     |                                    |
| method             | TEXT     | `isrc` / `search` / `manual`       |
| status             | TEXT     | `accepted` / `review` / `skipped` / `manual_accepted` / `manual_rejected` |
| score              | REAL     | 0.0 – 1.0                          |
| candidates_json    | TEXT     | JSON list of YTM candidates + scores |
| chosen_video_id    | TEXT     | the YTM videoId we'll import       |
| chosen_title       | TEXT     |                                    |
| chosen_artists     | TEXT     | comma-joined                       |
| chosen_duration_ms | INTEGER  |                                    |
| note               | TEXT     | human-readable breakdown           |

### `imported_tracks`
| column             | type     | notes                              |
| ------------------ | -------- | ---------------------------------- |
| id (PK)            | INTEGER  |                                    |
| playlist_id        | TEXT     |                                    |
| ytm_playlist_id    | TEXT     |                                    |
| video_id           | TEXT     |                                    |
| spotify_track_id   | TEXT     |                                    |
| imported_at        | DATETIME |                                    |

This table is the source of truth for resume. The importer filters out any
videoId already present for the target YTM playlist before re-adding.

### `auth_state`
| column     | type     | notes                          |
| ---------- | -------- | ------------------------------ |
| service (PK)| TEXT    | `spotify` / `ytm`              |
| ok         | INTEGER  |                                |
| detail     | TEXT     | last status message            |
| updated_at | DATETIME |                                |

## Matching strategy

For each Spotify track we try three strategies in order. The first one that
yields a usable result wins, but we always score + classify every candidate
so the review UI can show alternatives.

### Tier 1 — ISRC

If the Spotify track carries an `external_ids.isrc`, we ask YTM for it via
`search_by_isrc(isrc)`. YTM doesn't expose an ISRC field, so we use a quoted
search query and rely on the fact that the ISRC token itself is unique
enough to surface the right recording if it's in YTM at all.

The ISRC tier gets a **+0.15 confidence bonus** before classification. The
intent: when an ISRC query returns *any* candidate, we trust the recording
identity even if the title string is paraphrased (e.g. "Bohemian Rhapsody -
Remastered 2011" vs. Spotify's "Bohemian Rhapsody"). The bonus can never
push a totally unrelated result past the threshold — we still require some
title/artist/duration signal from the underlying scoring.

### Tier 2 — text search

`search(f"{title} {primary_artist}", filter="songs", limit=top_k)`. We
take up to `top_k` (default 5) candidates and score each.

### Scoring

Per the spec, a candidate scores points for:

| signal                             | weight |
| ---------------------------------- | -----: |
| Title matches exactly (normalized) |  +0.4  |
| Any artist substring match         |  +0.3  |
| Duration within ±3 s               |  +0.2  |
| Duration within ±10 s (else tier)  |  +0.1  |

"Normalized" means: NFKD, drop combining marks, lowercase, strip
punctuation, collapse whitespace. So "Café del Mar" matches "cafe del mar",
and "Bohemian Rhapsody - Remastered" matches "Bohemian Rhapsody
(Remastered)".

We add one small bonus that isn't in the spec: a partial-title token credit,
capped at +0.15 (Jaccard of word sets × 0.15). This lets a near-miss title
like "Clair de Lune (Live in Tokyo)" beat a totally wrong title without
ever being enough on its own to flip an obvious miss into an accept.

Total is capped at 1.0.

### Classification

- `score ≥ accept_threshold` (default 0.7) → **accepted** (auto-import)
- `review_threshold ≤ score < accept_threshold` (default 0.4) → **review**
  (shown in the UI for manual decision)
- `score < review_threshold` → **skipped**

The exact thresholds live in `config.toml` under `[matching]` and can be
overridden by environment variables `MIGRATOR_MATCH_THRESHOLD_ACCEPT` /
`_REVIEW`.

### Tier 3 — manual review

The `/review` page lists every row in the `review` bucket. For each row you
can:

- Click "Use this" on any of the suggested candidates → status becomes
  `accepted` (or `manual_accepted`).
- Search YTM manually with a free-text query and pick from the results.
- Click "No match" → status becomes `skipped`.
- Bulk-accept any review rows scoring above a custom threshold (default
  0.85) with one click.

Decisions are written through `apply_manual_decision()` and immediately
visible if you refresh.

## Concurrency model

Matching runs concurrently across Spotify tracks via `asyncio` with a
`Semaphore(concurrency)` cap (default 4). Each match is a synchronous call
to the YTM client wrapped in `asyncio.to_thread()` so the event loop stays
unblocked. The cap exists because YTM is rate-limited and aggressive
concurrency gets you 429s faster.

The CLI's `run` subcommand runs the whole pipeline synchronously: export →
match → (skip review) → import. The web UI's `/playlists/{id}/run-matching`
runs the same matching call from FastAPI, and `/playlists/{id}/import` calls
the same import function the CLI does.

## Logging

Two streams:

- `./logs/migrator.log` — JSON, one record per line, 2 MB rotation × 3
  backups. Designed for grep / jq.
- stdout — human-readable with a short timestamp and the logger name.
  Quiet-mode for `httpx`, `httpcore`, `urllib3` because they're noisy.

Every log record can carry arbitrary structured fields via the `extra={…}`
kwarg — these are flattened into the JSON output and ignored on stdout.

## Why these libraries?

- **Spotipy** is the de-facto Spotify Web API client and handles OAuth +
  pagination + rate-limit retries. We wrap it rather than calling
  `requests` directly so we get those for free.
- **ytmusicapi** speaks the same internal API the YouTube Music web client
  uses, so we don't have to maintain cookies / headers parsing. We *do*
  have to extract the auth headers manually because that's just how that
  library works — it's not a bug, the upstream project has an open issue
  tracking official OAuth.
- **sqlmodel** keeps models, queries, and the engine in one place. We
  don't use any of its migration / async features — SQLite + auto-create
  on startup is plenty for a personal tool.
- **keyring** + **cryptography** keep the Spotify refresh token out of
  plaintext without us rolling our own crypto. The Fernet fallback is for
  environments where keyring is genuinely unavailable.

## Out of scope (intentionally not built)

- Liked songs / saved albums / followed artists — playlists only.
- Two-way sync.
- Multi-user support.
- Mobile / responsive UI.
- Cloud deploy — `localhost` only.