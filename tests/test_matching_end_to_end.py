"""End-to-end matching tests against the three recorded fixtures.

These use the FakeYTMClient from conftest.py to replay real-shape YTM
responses captured in tests/fixtures/. The point is to make sure the full
pipeline (scripted YTM response → scoring → classification) lines up with
the expectations recorded in each fixture.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from migrator.matching import MatchRequest, Matcher

from .conftest import FakeYTMClient, load_fixture


def _request_from_fixture(sp: dict) -> MatchRequest:
    return MatchRequest(
        spotify_track_id=sp["track_id"],
        title=sp["name"],
        artists=sp["artists"],
        album=sp.get("album", ""),
        duration_ms=sp["duration_ms"],
        isrc=sp.get("isrc", ""),
    )


def _build_client(fx: dict) -> FakeYTMClient:
    scripted: dict[str, list[dict]] = {}
    if fx["spotify"].get("isrc"):
        scripted[f'isrc:{fx["spotify"]["isrc"]}'] = fx["ytm_candidates"]
    sp = fx["spotify"]
    scripted[f'{sp["name"]} {sp["artists"][0]}'] = fx["ytm_candidates"]
    return FakeYTMClient(scripted=scripted)


def _run(req: MatchRequest, client: FakeYTMClient) -> "MatchResult":  # type: ignore[name-defined]
    m = Matcher(client, concurrency=1)
    return asyncio.run(m.match_one(req))


def test_perfect_match_fixture():
    fx = load_fixture("perfect_match.json")
    req = _request_from_fixture(fx["spotify"])
    client = _build_client(fx)
    r = _run(req, client)

    assert r.status == fx["expected"]["status"], r
    assert r.score >= fx["expected"]["score_gte"], r
    assert r.chosen is not None
    assert r.chosen["videoId"] == fx["expected"]["chosen_video_id"]


def test_fuzzy_match_fixture():
    fx = load_fixture("fuzzy_match.json")
    req = _request_from_fixture(fx["spotify"])
    client = _build_client(fx)
    r = _run(req, client)

    lo, hi = fx["expected"]["score_between"]
    assert lo <= r.score <= hi, r
    assert r.status == fx["expected"]["status"], r
    assert r.chosen is not None
    assert r.chosen["videoId"] == fx["expected"]["chosen_video_id"]


def test_no_match_fixture():
    fx = load_fixture("no_match.json")
    req = _request_from_fixture(fx["spotify"])
    client = _build_client(fx)
    r = _run(req, client)

    assert r.score < fx["expected"]["score_lt"], r
    assert r.status == fx["expected"]["status"], r
    # chosen is None when no candidate clears the bar
    assert r.chosen is None or r.chosen["videoId"] != fx["ytm_candidates"][0]["videoId"]


# ---------- bulk pipeline ----------

def test_match_many_persists_results(isolated_settings):
    fx = load_fixture("perfect_match.json")
    sp = fx["spotify"]
    requests = [MatchRequest(
        spotify_track_id=f"t{i}",
        title=sp["name"],
        artists=sp["artists"],
        duration_ms=sp["duration_ms"],
        isrc=sp.get("isrc", ""),
    ) for i in range(3)]
    scripted = {
        f'isrc:{sp["isrc"]}': fx["ytm_candidates"],
        f'{sp["name"]} {sp["artists"][0]}': fx["ytm_candidates"],
    }
    client = FakeYTMClient(scripted=scripted)
    m = Matcher(client, concurrency=2)
    results = asyncio.run(m.match_many(requests))
    assert len(results) == 3
    assert all(r.status == "accepted" for r in results)


def test_match_many_serializes_ytm_calls_even_with_high_concurrency(
    isolated_settings, monkeypatch
) -> None:
    """Even with concurrency=8, the actual YTM calls must not overlap — this is
    what keeps the matcher from tripping YTM's anti-abuse limiter mid-batch.
    We simulate the overlap window with a threading.Lock held during each call
    and assert no two calls ever run concurrently."""
    import threading
    import time

    fx = load_fixture("perfect_match.json")
    sp = fx["spotify"]
    requests = [
        MatchRequest(
            spotify_track_id=f"t{i}",
            title=sp["name"],
            artists=sp["artists"],
            duration_ms=sp["duration_ms"],
            isrc="",  # skip tier 1 so we observe the text-search path
        )
        for i in range(8)
    ]
    scripted = {f'{sp["name"]} {sp["artists"][0]}': fx["ytm_candidates"]}
    client = FakeYTMClient(scripted=scripted)

    overlap = {"max_concurrent": 0, "current": 0}
    overlap_lock = threading.Lock()
    real_search = client.search_songs

    def tracking_search(query, limit=5):
        with overlap_lock:
            overlap["current"] += 1
            overlap["max_concurrent"] = max(overlap["max_concurrent"], overlap["current"])
        time.sleep(0.01)  # widen the window so a real overlap would be observable
        try:
            return real_search(query, limit)
        finally:
            with overlap_lock:
                overlap["current"] -= 1

    monkeypatch.setattr(client, "search_songs", tracking_search)
    m = Matcher(client, concurrency=8)
    asyncio.run(m.match_many(requests))

    assert overlap["max_concurrent"] == 1, (
        f"YTM calls overlapped (peak={overlap['max_concurrent']}); matcher is "
        f"still firing concurrent searches despite the lock"
    )


def test_match_one_uses_strict_search_when_configured(isolated_settings) -> None:
    """strict_search=True must route through search_songs_strict so a throttle
    is raised instead of silently degrading to []."""
    fx = load_fixture("perfect_match.json")
    sp = fx["spotify"]
    req = _request_from_fixture({**fx["spotify"], "isrc": ""})  # force tier-2
    scripted = {f'{sp["name"]} {sp["artists"][0]}': fx["ytm_candidates"]}
    client = FakeYTMClient(scripted=scripted)
    m = Matcher(client, concurrency=1, strict_search=True)
    asyncio.run(m.match_one(req))
    methods = [c[0] for c in client.calls]
    assert "search_strict" in methods
    assert "search" not in methods  # strict mode must not fall through silently