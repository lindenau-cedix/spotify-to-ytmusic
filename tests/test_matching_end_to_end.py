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