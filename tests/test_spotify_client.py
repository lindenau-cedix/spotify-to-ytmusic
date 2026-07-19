"""Tests for iter_playlist_tracks payload parsing.

Regression guard: the `/v1/playlists/{id}/items` endpoint returns each entry's
payload under `item` (the `track` key is deprecated and comes back null for
many accounts). If the parser only reads `track`, every playlist exports with
0 tracks. See src/migrator/spotify_client.py::iter_playlist_tracks.

Also covers the 429 retry behaviour: a single 429 mid-pagination must not
abort the playlist, and Retry-After must be honoured when present.
"""
from __future__ import annotations

import types

import pytest

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


# ---------- 429 retry behaviour ----------

import spotipy


class _ScriptedResp:
    """Minimal response stand-in: status, headers, json body, raise_for_status."""

    def __init__(self, status_code: int, body=None, *, reason: str = "OK", headers: dict | None = None):
        self.status_code = status_code
        self._body = body if body is not None else {"next": None, "items": []}
        self.ok = 200 <= status_code < 300
        self.reason = reason
        self.headers = headers or {}
        self.text = "" if self._body is None else str(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if not self.ok:
            import requests
            raise requests.HTTPError(f"{self.status_code} {self.reason}")


class _ScriptedEndpoint:
    """Like `_ScriptedResp` queue, but loops forever on 429/5xx until a 2xx
    is queued. Lets us prove "infinite retries" without an infinite list."""

    def __init__(self, success_status: int = 200, success_body=None,
                 throttle_status: int = 429, max_throttles_before_success: int = 1000):
        self.throttle_status = throttle_status
        self.success_status = success_status
        self.success_body = success_body if success_body is not None else {"next": None, "items": []}
        self.max = max_throttles_before_success
        self.throttled_count = 0

    def __call__(self, url, headers=None, params=None, timeout=None):
        if self.throttled_count >= self.max:
            return _ScriptedResp(self.success_status, body=self.success_body)
        self.throttled_count += 1
        return _ScriptedResp(self.throttle_status, reason="Too Many Requests")


def _patch_scripted(monkeypatch, responses: list[_ScriptedResp]):
    """Make `sc.requests.get` pop responses from `responses` on each call."""
    it = iter(responses)

    def _get(url, headers=None, params=None, timeout=None):
        return next(it)

    monkeypatch.setattr(sc.requests, "get", _get)


def test_retries_429_then_succeeds(isolated_settings, monkeypatch):
    """A single 429 mid-export must not abort the playlist — retry, then win."""
    sleeps: list[float] = []
    monkeypatch.setattr("migrator.spotify_client.time.sleep", lambda s: sleeps.append(s))

    ok_body = {"next": None, "items": [{"item": _track("t1")}]}
    _patch_scripted(monkeypatch, [
        _ScriptedResp(429, reason="Too Many Requests"),
        _ScriptedResp(200, body=ok_body),
    ])

    _sid, items = sc.iter_playlist_tracks(_fake_sp(), "PID")
    assert [t["id"] for t in items] == ["t1"]
    # One backoff before the retry that succeeded.
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(5.0)  # rate_limit_backoff_seconds * 2**0


def test_honours_retry_after_header(isolated_settings, monkeypatch):
    """Spotify's Retry-After (seconds) overrides exponential backoff."""
    sleeps: list[float] = []
    monkeypatch.setattr("migrator.spotify_client.time.sleep", lambda s: sleeps.append(s))

    ok_body = {"next": None, "items": []}
    _patch_scripted(monkeypatch, [
        _ScriptedResp(429, headers={"Retry-After": "12"}),
        _ScriptedResp(200, body=ok_body),
    ])

    sc.iter_playlist_tracks(_fake_sp(), "PID")
    assert sleeps == [pytest.approx(12.0)]


def test_retries_indefinitely_until_success(isolated_settings, monkeypatch):
    """The export must keep retrying on 429 — never give up on its own."""
    sleeps: list[float] = []
    monkeypatch.setattr("migrator.spotify_client.time.sleep", lambda s: sleeps.append(s))

    # 50 throttled responses in a row, then a 200. The endpoint caps throttles
    # at 1000 so a real infinite loop would never terminate — that's the point.
    endpoint = _ScriptedEndpoint(
        throttle_status=429, max_throttles_before_success=50,
        success_body={"next": None, "items": [{"item": _track("t1")}]},
    )
    monkeypatch.setattr(sc.requests, "get", endpoint)

    sid, items = sc.iter_playlist_tracks(_fake_sp(), "PID")
    assert [t["id"] for t in items] == ["t1"]
    assert endpoint.throttled_count == 50
    assert len(sleeps) == 50


def test_backoff_caps_at_max_backoff_seconds(isolated_settings, monkeypatch):
    """Long Retry-After (or runaway exponential backoff) is clamped to the cap."""
    sleeps: list[float] = []
    monkeypatch.setattr("migrator.spotify_client.time.sleep", lambda s: sleeps.append(s))

    # Shrink the cap so the test doesn't have to wait; also shrink base so we
    # can hit the cap quickly (5 attempts at base 1.0 with cap 3.0).
    monkeypatch.setattr(
        "migrator.spotify_client.get_settings",
        lambda: _CfgSpy(backoff=1.0, max_backoff=3.0),
    )

    ok_body = {"next": None, "items": []}
    # Lots of throttles so exponential backoff would otherwise blow past the cap.
    _patch_scripted(monkeypatch, [_ScriptedResp(429)] * 5 + [_ScriptedResp(200, body=ok_body)])

    sc.iter_playlist_tracks(_fake_sp(), "PID")
    # base=1.0 → attempts compute 1, 2, 4, 8, 16 → clamped to 3 each. No Retry-After
    # header, so the computed backoff (clamped) wins.
    assert sleeps == [pytest.approx(1.0), pytest.approx(2.0), pytest.approx(3.0),
                      pytest.approx(3.0), pytest.approx(3.0)]


def test_retry_after_clamped_to_cap(isolated_settings, monkeypatch):
    """A huge Retry-After (e.g. 9999s) must be clamped to the configured cap."""
    sleeps: list[float] = []
    monkeypatch.setattr("migrator.spotify_client.time.sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(
        "migrator.spotify_client.get_settings",
        lambda: _CfgSpy(backoff=5.0, max_backoff=30.0),
    )

    ok_body = {"next": None, "items": []}
    _patch_scripted(monkeypatch, [
        _ScriptedResp(429, headers={"Retry-After": "9999"}),
        _ScriptedResp(200, body=ok_body),
    ])

    sc.iter_playlist_tracks(_fake_sp(), "PID")
    assert sleeps == [pytest.approx(30.0)]  # clamped, not 9999


def test_retry_does_not_affect_4xx_other_than_429(isolated_settings, monkeypatch):
    """A plain 404 must NOT be retried — surface it on the first call."""
    sleeps: list[float] = []
    monkeypatch.setattr("migrator.spotify_client.time.sleep", lambda s: sleeps.append(s))

    _patch_scripted(monkeypatch, [_ScriptedResp(404, reason="Not Found")])

    with pytest.raises(spotipy.SpotifyException) as exc_info:
        sc.iter_playlist_tracks(_fake_sp(), "PID")
    assert exc_info.value.http_status == 404
    assert sleeps == []  # no backoff sleeps — 404 is not retryable


def test_retries_transient_5xx(isolated_settings, monkeypatch):
    """A 503 (transient backend error) is retried indefinitely just like 429."""
    sleeps: list[float] = []
    monkeypatch.setattr("migrator.spotify_client.time.sleep", lambda s: sleeps.append(s))

    ok_body = {"next": None, "items": [{"item": _track("t1")}]}
    _patch_scripted(monkeypatch, [
        _ScriptedResp(503, reason="Service Unavailable"),
        _ScriptedResp(503, reason="Service Unavailable"),
        _ScriptedResp(200, body=ok_body),
    ])

    _sid, items = sc.iter_playlist_tracks(_fake_sp(), "PID")
    assert [t["id"] for t in items] == ["t1"]
    assert len(sleeps) == 2


def test_retry_across_pagination(isolated_settings, monkeypatch):
    """429s on different pages all get retried independently — only final 200s stick."""
    sleeps: list[float] = []
    monkeypatch.setattr("migrator.spotify_client.time.sleep", lambda s: sleeps.append(s))

    # Page 1: 429 → 200 (with next page URL).
    page1 = {
        "next": "https://api.spotify.com/v1/playlists/PID/items?offset=100&limit=100",
        "items": [{"item": _track("t1")}],
        "snapshot_id": "snap",
    }
    # Page 2: 429 → 429 → 200 (last page).
    page2 = {"next": None, "items": [{"item": _track("t2")}]}
    _patch_scripted(monkeypatch, [
        _ScriptedResp(429),
        _ScriptedResp(200, body=page1),
        _ScriptedResp(429),
        _ScriptedResp(429),
        _ScriptedResp(200, body=page2),
    ])

    sid, items = sc.iter_playlist_tracks(_fake_sp(), "PID")
    assert sid == "snap"
    assert [t["id"] for t in items] == ["t1", "t2"]
    # 3 sleeps total: one before each retry that ultimately succeeded.
    assert len(sleeps) == 3


class _CfgSpy:
    """Minimal stand-in for the Settings object the retry helper reads.

    The helper only touches `s.toml.spotify.{rate_limit_backoff_seconds,
    rate_limit_max_backoff_seconds}`, so a thin shim is enough.
    """

    def __init__(self, backoff: float, max_backoff: float):
        self.toml = type("T", (), {"spotify": type(
            "S", (), {
                "rate_limit_backoff_seconds": backoff,
                "rate_limit_max_backoff_seconds": max_backoff,
            },
        )()})()
