"""Tests for iter_playlist_tracks payload parsing.

Regression guard: the `/v1/playlists/{id}/items` endpoint returns each entry's
payload under `item` (the `track` key is deprecated and comes back null for
many accounts). If the parser only reads `track`, every playlist exports with
0 tracks. See src/migrator/spotify_client.py::iter_playlist_tracks.
"""
from __future__ import annotations

import types

import migrator.spotify_client as sc


def _fake_sp():
    return types.SimpleNamespace(
        auth_manager=types.SimpleNamespace(
            get_access_token=lambda: {"access_token": "TOK"}
        )
    )


def _patch_response(monkeypatch, payload):
    class _Resp:
        ok = True
        status_code = 200
        reason = "OK"

        def json(self):
            return payload

    monkeypatch.setattr(
        sc.requests, "get",
        lambda url, headers=None, params=None, timeout=None: _Resp(),
    )


def _track(tid):
    return {
        "id": tid,
        "name": f"Song {tid}",
        "duration_ms": 200000,
        "artists": [{"name": "Artist"}],
        "album": {"name": "Album"},
        "external_ids": {"isrc": f"ISRC{tid}"},
        "external_urls": {"spotify": f"url/{tid}"},
    }


def test_parses_modern_item_field(monkeypatch):
    """Modern shape: payload under `item`, deprecated `track` is null."""
    _patch_response(monkeypatch, {
        "next": None,
        "items": [
            {"is_local": False, "track": None, "item": _track("t1")},
            {"is_local": False, "track": None, "item": _track("t2")},
        ],
    })
    _sid, items = sc.iter_playlist_tracks(_fake_sp(), "PID")
    assert [t["id"] for t in items] == ["t1", "t2"]


def test_parses_legacy_track_field(monkeypatch):
    """Legacy shape: payload only under `track` (no `item` key)."""
    _patch_response(monkeypatch, {
        "next": None,
        "items": [{"track": _track("t1")}, {"track": _track("t2")}],
    })
    _sid, items = sc.iter_playlist_tracks(_fake_sp(), "PID")
    assert [t["id"] for t in items] == ["t1", "t2"]


def test_skips_episodes_and_local_files(monkeypatch):
    """Null payloads (episodes) and id-less entries (local files) are dropped."""
    _patch_response(monkeypatch, {
        "next": None,
        "items": [
            {"track": None, "item": None},               # episode / unresolvable
            {"item": {"id": None, "name": "local"}},      # local file, no id
            {"item": _track("keep")},                      # real track survives
        ],
    })
    _sid, items = sc.iter_playlist_tracks(_fake_sp(), "PID")
    assert [t["id"] for t in items] == ["keep"]
