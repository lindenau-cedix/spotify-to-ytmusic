"""Tests for `migrator accept` — the bulk "accept all reviews" command.

Covers the db helper (`accept_reviews`) and the CLI wrapper around it. The
contract worth pinning: only `review` rows in the *current* snapshot flip to
`accepted`, and only when they actually carry a `chosen_video_id`.
"""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from migrator import db as db_module
from migrator.cli import app
from migrator.db import Matches, Playlists, Tracks, accept_reviews, get_session

runner = CliRunner()


@pytest.fixture(autouse=True)
def _fresh_engine(isolated_settings):
    """`db._engine` is a module-level cache; reset it so each test gets a clean DB."""
    db_module._engine = None
    yield
    db_module._engine = None


def _seed(
    *,
    playlist_id: str = "PL1",
    snapshot_id: str = "SNAP1",
    name: str = "Playlist One",
    matches: list[tuple[str, str, float, str | None]],
) -> None:
    """Insert a playlist plus one (track, match) pair per `matches` entry.

    Each entry is `(spotify_track_id, status, score, chosen_video_id)`.
    """
    with get_session() as s:
        s.add(
            Playlists(
                playlist_id=playlist_id,
                snapshot_id=snapshot_id,
                name=name,
                track_count=len(matches),
            )
        )
        for i, (track_id, status, score, video_id) in enumerate(matches):
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
                    status=status,
                    score=score,
                    chosen_video_id=video_id,
                )
            )
        s.commit()


def _statuses(playlist_id: str = "PL1") -> dict[str, str]:
    with get_session() as s:
        rows = s.query(Matches).filter(Matches.playlist_id == playlist_id).all()
        return {m.spotify_track_id: m.status for m in rows}


# ---------- db helper ----------


def test_accept_reviews_flips_only_review_rows():
    _seed(
        matches=[
            ("T1", "review", 0.7, "vid1"),
            ("T2", "accepted", 0.95, "vid2"),
            ("T3", "skipped", 0.1, None),
        ]
    )
    summaries = accept_reviews()
    assert [(s.playlist_id, s.accepted, s.no_candidate) for s in summaries] == [
        ("PL1", 1, 0)
    ]
    assert _statuses() == {"T1": "accepted", "T2": "accepted", "T3": "skipped"}


def test_accept_reviews_leaves_rows_without_a_candidate():
    _seed(
        matches=[
            ("T1", "review", 0.7, "vid1"),
            ("T2", "review", 0.6, None),  # nothing to import — must stay in review
        ]
    )
    (summary,) = accept_reviews()
    assert (summary.accepted, summary.no_candidate) == (1, 1)
    assert _statuses() == {"T1": "accepted", "T2": "review"}


def test_accept_reviews_honours_threshold():
    _seed(matches=[("T1", "review", 0.9, "vid1"), ("T2", "review", 0.5, "vid2")])
    (summary,) = accept_reviews(threshold=0.85)
    assert summary.accepted == 1
    assert _statuses() == {"T1": "accepted", "T2": "review"}


def test_accept_reviews_dry_run_writes_nothing():
    _seed(matches=[("T1", "review", 0.7, "vid1")])
    (summary,) = accept_reviews(dry_run=True)
    assert summary.accepted == 1
    assert _statuses() == {"T1": "review"}


def test_accept_reviews_ignores_stale_snapshots():
    _seed(matches=[("T1", "review", 0.7, "vid1")])
    with get_session() as s:
        s.add(
            Matches(
                playlist_id="PL1",
                snapshot_id="SNAP_OLD",
                spotify_track_id="T_OLD",
                method="search",
                status="review",
                score=0.9,
                chosen_video_id="vid_old",
            )
        )
        s.commit()
    (summary,) = accept_reviews()
    assert summary.accepted == 1
    assert _statuses()["T_OLD"] == "review"


def test_accept_reviews_scoped_to_one_playlist():
    _seed(matches=[("T1", "review", 0.7, "vid1")])
    _seed(
        playlist_id="PL2",
        snapshot_id="SNAP2",
        name="Playlist Two",
        matches=[("T2", "review", 0.7, "vid2")],
    )
    (summary,) = accept_reviews("PL1")
    assert summary.playlist_id == "PL1"
    assert _statuses("PL1") == {"T1": "accepted"}
    assert _statuses("PL2") == {"T2": "review"}


def test_accept_reviews_unknown_playlist_is_empty():
    _seed(matches=[("T1", "review", 0.7, "vid1")])
    assert accept_reviews("NOPE") == []
    assert _statuses() == {"T1": "review"}


# ---------- CLI ----------


def test_accept_cmd_accepts_with_yes():
    _seed(matches=[("T1", "review", 0.7, "vid1"), ("T2", "review", 0.8, "vid2")])
    res = runner.invoke(app, ["accept", "--yes"])
    assert res.exit_code == 0, res.output
    assert "Accepted 2 match(es)" in res.output
    assert _statuses() == {"T1": "accepted", "T2": "accepted"}


def test_accept_cmd_prompts_and_aborts_on_no():
    _seed(matches=[("T1", "review", 0.7, "vid1")])
    res = runner.invoke(app, ["accept"], input="n\n")
    assert res.exit_code != 0
    assert _statuses() == {"T1": "review"}


def test_accept_cmd_dry_run_writes_nothing():
    _seed(matches=[("T1", "review", 0.7, "vid1")])
    res = runner.invoke(app, ["accept", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "would accept 1" in res.output
    assert _statuses() == {"T1": "review"}


def test_accept_cmd_nothing_to_do_exits_zero():
    _seed(matches=[("T1", "accepted", 0.9, "vid1")])
    res = runner.invoke(app, ["accept", "--yes"])
    assert res.exit_code == 0, res.output
    assert "No review matches to accept" in res.output


def test_accept_cmd_unknown_playlist_exits_one():
    _seed(matches=[("T1", "review", 0.7, "vid1")])
    res = runner.invoke(app, ["accept", "NOPE", "--yes"])
    assert res.exit_code == 1
    assert _statuses() == {"T1": "review"}


def test_accept_cmd_threshold_option():
    _seed(matches=[("T1", "review", 0.9, "vid1"), ("T2", "review", 0.5, "vid2")])
    res = runner.invoke(app, ["accept", "--threshold", "0.85", "--yes"])
    assert res.exit_code == 0, res.output
    assert _statuses() == {"T1": "accepted", "T2": "review"}


def test_accept_appears_in_help():
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0
    assert "accept" in res.output
