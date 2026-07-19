"""Tests for the two duplicate defences in `_import_one`.

YouTube's playlist edit is atomic on duplicates: one repeated videoId makes it
answer STATUS_FAILED and add *nothing* from that batch. At `batch_size = 50`
that means a single collision costs 50 good tracks. Two independent sources fed
it, and both are guarded here:

1. `todo` was a plain list, so a matcher that mapped two Spotify tracks onto one
   YouTube video (album + single releases, or a track duplicated inside the
   Spotify playlist) put the same videoId in a batch twice.
2. `imported_tracks` records only *confirmed* adds, so a run killed mid-playlist
   left tracks live on YTM that we never recorded — and re-sent every run after.

Observed before the fix: three playlists whose YTM playlists were empty imported
0 of 43, 34 and 18 tracks, because their batches carried 6, 1 and 6 duplicates.
"""
from __future__ import annotations

import pytest
import typer

from migrator import cli, db as db_module
from migrator.db import ImportedTracks, Matches, Playlists, Tracks, get_session


@pytest.fixture(autouse=True)
def _fresh_engine(isolated_settings):
    db_module._engine = None
    yield
    db_module._engine = None


def _seed(
    *,
    video_ids: list[str],
    playlist_id: str = "PL1",
    snapshot_id: str = "SNAP1",
    ytm_playlist_id: str | None = "YTM1",
    imported: list[str] | None = None,
) -> None:
    """One accepted match per entry in `video_ids` (repeats are the point)."""
    with get_session() as s:
        s.add(
            Playlists(
                playlist_id=playlist_id,
                snapshot_id=snapshot_id,
                name="Playlist One",
                track_count=len(video_ids),
                ytm_playlist_id=ytm_playlist_id,
            )
        )
        for i, vid in enumerate(video_ids):
            track_id = f"sp{i}"
            s.add(
                Tracks(
                    playlist_id=playlist_id,
                    snapshot_id=snapshot_id,
                    position=i,
                    spotify_track_id=track_id,
                    name=f"Track {i}",
                    artists='["Artist"]',
                    duration_ms=180_000,
                )
            )
            s.add(
                Matches(
                    playlist_id=playlist_id,
                    snapshot_id=snapshot_id,
                    spotify_track_id=track_id,
                    method="search",
                    status="accepted",
                    score=0.9,
                    chosen_video_id=vid,
                )
            )
        for vid in imported or []:
            s.add(
                ImportedTracks(
                    playlist_id=playlist_id,
                    ytm_playlist_id=ytm_playlist_id,
                    video_id=vid,
                    spotify_track_id="seed",
                )
            )
        s.commit()


def _run_import(fake_ytm_client) -> None:
    cli._import_one(fake_ytm_client, "PL1", dry_run=False, yes_assumed=True)


def _sent(fake_ytm_client) -> list[str]:
    return getattr(fake_ytm_client, "sent_video_ids", [])


def test_duplicate_video_ids_are_collapsed(fake_ytm_client) -> None:
    """Two Spotify tracks matching one video must be sent once, not twice.

    Sending it twice fails the whole batch, so this single duplicate would have
    cost every track batched alongside it.
    """
    _seed(video_ids=["a", "b", "a", "c"])

    _run_import(fake_ytm_client)

    assert _sent(fake_ytm_client) == ["a", "b", "c"]


def test_dedupe_preserves_playlist_order(fake_ytm_client) -> None:
    """Order is the user's playlist order — dedupe must keep the first position."""
    _seed(video_ids=["c", "a", "c", "b", "a"])

    _run_import(fake_ytm_client)

    assert _sent(fake_ytm_client) == ["c", "a", "b"]


def test_tracks_live_on_ytm_are_not_resent(fake_ytm_client) -> None:
    """The `:P` case: 70 tracks live on YTM, only 20 recorded, so all 50 in the
    delta were already present and every batch failed."""
    _seed(video_ids=["a", "b", "c"], imported=["a"])
    fake_ytm_client.live_video_ids = {"a", "b"}  # b landed but was never recorded

    _run_import(fake_ytm_client)

    assert _sent(fake_ytm_client) == ["c"]


def test_reconciliation_backfills_imported_tracks(fake_ytm_client) -> None:
    """Recording the recovered tracks is what stops the cycle repeating.

    Without the backfill the next run re-reads the playlist and re-derives the
    same delta; with it, `imported_tracks` finally matches reality.
    """
    _seed(video_ids=["a", "b", "c"], imported=["a"])
    fake_ytm_client.live_video_ids = {"a", "b"}

    _run_import(fake_ytm_client)

    with get_session() as s:
        recorded = {r.video_id for r in s.query(ImportedTracks).all()}
    assert recorded == {"a", "b", "c"}


def test_reconciliation_ignores_foreign_tracks(fake_ytm_client) -> None:
    """Tracks on the YTM playlist that we never chose aren't ours to claim.

    A user may have added songs by hand. They must stay out of `imported_tracks`
    (we have no Spotify id for them) but still stay out of the batch, since
    sending one would fail the whole edit.
    """
    _seed(video_ids=["a", "b"])
    fake_ytm_client.live_video_ids = {"a", "zz_user_added"}

    _run_import(fake_ytm_client)

    assert _sent(fake_ytm_client) == ["b"]
    with get_session() as s:
        recorded = {r.video_id for r in s.query(ImportedTracks).all()}
    assert "zz_user_added" not in recorded


def test_fully_imported_playlist_sends_nothing(fake_ytm_client) -> None:
    """Everything live on YTM → nothing to do, and no add call at all."""
    _seed(video_ids=["a", "b"])
    fake_ytm_client.live_video_ids = {"a", "b"}

    with pytest.raises(typer.Exit):  # "Already fully imported."
        _run_import(fake_ytm_client)

    assert not any(c[0] == "add_playlist_items" for c in fake_ytm_client.calls)


def test_unreadable_playlist_falls_back_to_recorded_set(fake_ytm_client) -> None:
    """If the live read fails we must not treat the playlist as empty.

    `playlist_video_ids` returns an empty set on error, which here is
    indistinguishable from a genuinely empty playlist — so the recorded set has
    to remain authoritative rather than being replaced by it.
    """
    _seed(video_ids=["a", "b"], imported=["a"])
    fake_ytm_client.live_video_ids = set()  # read failed

    _run_import(fake_ytm_client)

    assert _sent(fake_ytm_client) == ["b"]  # "a" still excluded


def test_dry_run_skips_live_read(fake_ytm_client) -> None:
    """A dry run makes no writes, so it must not spend a network read either."""
    _seed(video_ids=["a", "b"])

    cli._import_one(fake_ytm_client, "PL1", dry_run=True, yes_assumed=True)

    assert not any(c[0] == "playlist_video_ids" for c in fake_ytm_client.calls)
