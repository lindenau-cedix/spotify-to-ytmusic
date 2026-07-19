"""Regression tests for the wipe helpers and the incremental `run` flow.

Two contracts live here:

1. The wipe helpers in ``db.py`` (``clear_all_matches``,
   ``clear_all_imported_tracks``, ``reset_all_playlist_progress``) work as
   advertised and stay idempotent — the CLI doesn't call them anymore, but
   they're still useful utilities and a future command might.

2. ``python -m migrator run`` is incremental: the first invocation exports
   Spotify playlists; every later invocation skips the export and just
   imports whatever matches are already present. ``import_cmd`` is the
   single source of truth for import behavior, so the test verifies it
   gets called — not that matches get pushed to YTM.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import typer

from migrator import cli, db as db_module
from migrator.db import (
    ImportedTracks,
    Matches,
    Playlists,
    Tracks,
    clear_all_imported_tracks,
    clear_all_matches,
    get_session,
    reset_all_playlist_progress,
)
from tests.conftest import FakeYTMClient


@pytest.fixture(autouse=True)
def _fresh_engine(isolated_settings):
    """`isolated_settings` resets config paths but `db._engine` is a module-level
    cache — once any test primes it, later tests in the same session hit the
    same DB file. Reset the cache so every test gets a clean tmp DB.
    """
    db_module._engine = None
    yield
    db_module._engine = None


# ---------- helpers ----------


def _seed_playlist(
    *,
    playlist_id: str = "PL_TEST",
    snapshot_id: str = "SNAP_TEST",
    ytm_playlist_id: str | None = "PL_OLD_YTM",
) -> Playlists:
    """Insert a Playlists row (and a Tracks row so matches have something to FK against)."""
    with get_session() as s:
        pl = Playlists(
            playlist_id=playlist_id,
            snapshot_id=snapshot_id,
            name="Test Playlist",
            track_count=1,
            ytm_playlist_id=ytm_playlist_id,
            imported_at=datetime.now(timezone.utc),
        )
        s.add(pl)
        s.add(
            Tracks(
                playlist_id=playlist_id,
                snapshot_id=snapshot_id,
                position=0,
                spotify_track_id="TRACK_1",
                name="Track 1",
                artists='["Artist 1"]',
                album="Album 1",
                duration_ms=180000,
                isrc="ISRC_1",
                spotify_url="",
                raw_json="{}",
            )
        )
        s.commit()
    return pl


def _seed_match(
    *, playlist_id: str, snapshot_id: str, track_id: str, status: str
) -> None:
    with get_session() as s:
        s.add(
            Matches(
                playlist_id=playlist_id,
                snapshot_id=snapshot_id,
                spotify_track_id=track_id,
                method="search",
                status=status,
                score=0.5,
                candidates_json="[]",
                chosen_video_id=f"ytm_{track_id}",
                chosen_title="x",
                chosen_artists="x",
                chosen_duration_ms=180000,
            )
        )
        s.commit()


def _seed_imported(playlist_id: str, ytm_playlist_id: str, video_id: str) -> None:
    with get_session() as s:
        s.add(
            ImportedTracks(
                playlist_id=playlist_id,
                ytm_playlist_id=ytm_playlist_id,
                video_id=video_id,
                spotify_track_id=video_id,
            )
        )
        s.commit()


# ---------- wipe helpers (still useful as utilities) ----------


def test_run_wipe_clears_progress(isolated_settings) -> None:
    """Pre-populate every wipe target, then run the three helpers and assert
    matches/imported_tracks are empty and `ytm_playlist_id`/`imported_at` are NULL.
    """
    _seed_playlist()
    _seed_match(playlist_id="PL_TEST", snapshot_id="SNAP_TEST",
                track_id="TRACK_1", status="accepted")
    _seed_imported("PL_TEST", "PL_OLD_YTM", "ytm_TRACK_1")

    n_m = clear_all_matches()
    n_i = clear_all_imported_tracks()
    n_p = reset_all_playlist_progress()

    with get_session() as s:
        from sqlmodel import select

        matches_left = s.exec(select(Matches)).all()
        imported_left = s.exec(select(ImportedTracks)).all()
        pl = s.get(Playlists, "PL_TEST")

    assert matches_left == []
    assert imported_left == []
    assert pl is not None
    assert pl.ytm_playlist_id is None
    assert pl.imported_at is None
    assert n_m == 1
    assert n_i == 1
    assert n_p == 1


def test_wipe_helpers_are_idempotent_when_tables_empty(isolated_settings) -> None:
    """Second wipe returns 0 — guards against an accidental "I forgot to
    commit" regression in the helpers, where the rowcount might still report
    the pre-wipe count after the wipe.
    """
    n_m = clear_all_matches()
    n_i = clear_all_imported_tracks()
    n_p = reset_all_playlist_progress()
    assert n_m == 0
    assert n_i == 0
    assert n_p == 0


def test_reset_all_playlist_progress_leaves_descriptive_fields_intact(
    isolated_settings,
) -> None:
    """`reset_all_playlist_progress` should only null progress columns and
    bump `updated_at`; it must NOT erase `name`/`description`/`track_count`/etc.
    """
    with get_session() as s:
        s.add(
            Playlists(
                playlist_id="PL_KEEP",
                snapshot_id="SNAP_OLD",
                name="Keep Name",
                description="Keep desc",
                owner="owner@example",
                track_count=42,
                ytm_playlist_id="PL_OLD",
                imported_at=datetime.now(timezone.utc),
            )
        )
        s.commit()

    reset_all_playlist_progress()

    with get_session() as s:
        pl = s.get(Playlists, "PL_KEEP")
    assert pl is not None
    assert pl.name == "Keep Name"
    assert pl.description == "Keep desc"
    assert pl.owner == "owner@example"
    assert pl.track_count == 42
    assert pl.ytm_playlist_id is None
    assert pl.imported_at is None


# ---------- incremental `run` ----------


def test_run_exports_when_db_is_empty(isolated_settings, monkeypatch) -> None:
    """First run (no exported playlists): must call export + import_cmd.
    Auth must be checked at this stage, because export needs Spotify creds.
    """
    calls: dict[str, list] = {"export": [], "import_cmd": []}

    monkeypatch.setattr(
        cli, "export",
        lambda *, playlist_id, dry_run: calls["export"].append((playlist_id, dry_run))
        or _seed_playlist(),
    )
    monkeypatch.setattr(
        cli, "import_cmd",
        lambda *, playlist_id, dry_run, yes: calls["import_cmd"].append(
            (playlist_id, dry_run, yes)
        ),
    )

    # Stub auth so we don't need real credentials for this regression.
    monkeypatch.setattr(cli, "auth_ok", lambda service: True)

    cli.run(playlist_id=None, dry_run=False)

    assert calls["export"] == [(None, False)]
    # Single-seeded playlist should drive exactly one import_cmd call.
    assert calls["import_cmd"] == [("PL_TEST", False, True)]


def test_run_skips_export_when_data_already_present(
    isolated_settings, monkeypatch
) -> None:
    """Subsequent run (playlists already exported): must NOT call export,
    just import. Progress columns must survive untouched.
    """
    _seed_playlist()  # existing data → "not first run"

    export_calls: list = []

    monkeypatch.setattr(
        cli, "export",
        lambda *, playlist_id, dry_run: export_calls.append((playlist_id, dry_run)),
    )

    import_calls: list = []
    monkeypatch.setattr(
        cli, "import_cmd",
        lambda *, playlist_id, dry_run, yes: import_calls.append(
            (playlist_id, dry_run, yes)
        ),
    )
    monkeypatch.setattr(cli, "auth_ok", lambda service: True)

    cli.run(playlist_id=None, dry_run=False)

    assert export_calls == [], "export must be skipped on subsequent runs"
    assert import_calls == [("PL_TEST", False, True)]

    # Progress columns are preserved across runs.
    with get_session() as s:
        pl = s.get(Playlists, "PL_TEST")
    assert pl is not None
    assert pl.ytm_playlist_id == "PL_OLD_YTM"
    assert pl.imported_at is not None


def test_run_with_explicit_playlist_id_skips_export(
    isolated_settings, monkeypatch
) -> None:
    """When the user names a playlist, subsequent runs (data exists) must
    still skip export and just import the named playlist — don't re-export
    the whole library.
    """
    _seed_playlist()

    export_calls: list = []
    import_calls: list = []
    monkeypatch.setattr(
        cli, "export",
        lambda *, playlist_id, dry_run: export_calls.append((playlist_id, dry_run)),
    )
    monkeypatch.setattr(
        cli, "import_cmd",
        lambda *, playlist_id, dry_run, yes: import_calls.append(
            (playlist_id, dry_run, yes)
        ),
    )
    monkeypatch.setattr(cli, "auth_ok", lambda service: True)

    cli.run(playlist_id="PL_TEST", dry_run=False)

    assert export_calls == []
    # Single-playlist path delegates directly to import_cmd.
    assert import_calls == [("PL_TEST", False, True)]


def test_run_exits_cleanly_when_export_produces_no_playlists(
    isolated_settings, monkeypatch
) -> None:
    """If the first-run export finds nothing to export (e.g. empty Spotify
    library, or every playlist was unreadable), the command must exit 0
    without falling through to the import step.
    """
    monkeypatch.setattr(
        cli, "export",
        lambda *, playlist_id, dry_run: None,  # writes nothing
    )
    import_calls: list = []
    monkeypatch.setattr(
        cli, "import_cmd",
        lambda *, playlist_id, dry_run, yes: import_calls.append(
            (playlist_id, dry_run, yes)
        ),
    )
    monkeypatch.setattr(cli, "auth_ok", lambda service: True)

    try:
        cli.run(playlist_id=None, dry_run=False)
    except typer.Exit as e:
        code = getattr(e, "exit_code", getattr(e, "code", None))
        assert code in (None, 0), f"unexpected non-zero exit: {code}"

    assert import_calls == [], "import must not run when there is nothing to import"
