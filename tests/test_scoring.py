"""Table-driven tests for the matching scoring function.

The scoring function is the single most important piece of logic in this
tool. These tests pin down the exact thresholds and combinations so a
refactor can't silently change acceptance behavior.
"""
from __future__ import annotations

import pytest

from migrator.matching import classify, score_candidate


# ---------- normalize + classify trivials ----------

def test_classify_thresholds():
    assert classify(0.95, accept=0.7, review=0.4) == "accepted"
    assert classify(0.7, accept=0.7, review=0.4) == "accepted"
    assert classify(0.69, accept=0.7, review=0.4) == "review"
    assert classify(0.4, accept=0.7, review=0.4) == "review"
    assert classify(0.39, accept=0.7, review=0.4) == "skipped"
    assert classify(0.0, accept=0.7, review=0.4) == "skipped"


# ---------- scenario table ----------

SCENARIOS = [
    # name, expected_components
    pytest.param(
        "Perfect match: same title + artist + duration within 1s",
        dict(
            spotify_title="Bohemian Rhapsody",
            spotify_artist="Queen",
            spotify_duration_ms=354_947,
            cand=dict(
                title="Bohemian Rhapsody",
                artists=[{"name": "Queen"}],
                duration="5:55", duration_seconds=355.0,
            ),
            want_total_gte=0.9,
            want_title_exact=0.4,
            want_artist_match=0.3,
            want_duration_strict=0.2,
        ),
        id="perfect",
    ),
    pytest.param(
        "Title case + diacritics are ignored",
        dict(
            spotify_title="Café del Mar",
            spotify_artist="José Padilla",
            spotify_duration_ms=360_000,
            cand=dict(
                title="cafe del mar",
                artists=[{"name": "jose padilla"}],
                duration="6:00", duration_seconds=360.0,
            ),
            want_total_gte=0.9,
        ),
        id="case-and-diacritics",
    ),
    pytest.param(
        "Wrong title + wrong artist + wrong duration → 0",
        dict(
            spotify_title="Foo",
            spotify_artist="Bar",
            spotify_duration_ms=100_000,
            cand=dict(
                title="Something Else",
                artists=[{"name": "Other"}],
                duration="9:99",  # invalid
            ),
            want_total_eq=0.0,
        ),
        id="all-mismatch",
    ),
    pytest.param(
        "Title wrong but artist matches + duration strict",
        dict(
            spotify_title="Original Mix",
            spotify_artist="Daft Punk",
            spotify_duration_ms=240_000,
            cand=dict(
                title="Extended Club Mix",
                artists=[{"name": "Daft Punk"}],
                duration="4:00", duration_seconds=240.0,
            ),
            want_artist_match=0.3,
            want_duration_strict=0.2,
            want_total_lt=0.7,
        ),
        id="artist-only-no-title",
    ),
    pytest.param(
        "Title exact but no artist match",
        dict(
            spotify_title="Around the World",
            spotify_artist="Daft Punk",
            spotify_duration_ms=420_000,
            cand=dict(
                title="Around the World",
                artists=[{"name": "Someone Else"}],
                duration="7:00", duration_seconds=420.0,
            ),
            want_title_exact=0.4,
            want_duration_strict=0.2,
            want_total_between=(0.5, 0.9),
        ),
        id="title-exact-no-artist",
    ),
    pytest.param(
        "Duration within loose but not strict tolerance",
        dict(
            spotify_title="X",
            spotify_artist="Y",
            spotify_duration_ms=200_000,
            cand=dict(
                title="X",
                artists=[{"name": "Y"}],
                duration="3:30", duration_seconds=210.0,
            ),
            want_duration_strict=0.0,
            want_duration_loose=0.1,
        ),
        id="duration-loose-only",
    ),
    pytest.param(
        "Total score cannot exceed 1.0 (artist multi-match shouldn't compound)",
        dict(
            spotify_title="X",
            spotify_artist="Y",
            spotify_duration_ms=100_000,
            cand=dict(
                title="X",
                artists=[{"name": "Y"}, {"name": "Y2"}, {"name": "Y3"}],
                duration="1:40", duration_seconds=100.0,
            ),
            want_total_eq=0.9,  # 0.4 + 0.3 + 0.2 exactly, never more
        ),
        id="max-score-cap",
    ),
    pytest.param(
        "Partial-title credit beats obvious wrong title",
        dict(
            spotify_title="Clair de Lune",
            spotify_artist="Debussy",
            spotify_duration_ms=300_000,
            cand=dict(
                title="Clair de Lune (Live in Tokyo)",
                artists=[{"name": "Debussy"}],
                duration="5:00", duration_seconds=300.0,
            ),
            want_title_exact=0.0,
            want_artist_match=0.3,
            want_duration_strict=0.2,
            want_title_tokens_gt=0.0,
            want_total_between=(0.4, 0.8),
        ),
        id="partial-title-credit",
    ),
]


@pytest.mark.parametrize("name, scenario", SCENARIOS)
def test_scoring_table(name, scenario):
    s = scenario
    br = score_candidate(
        s["spotify_title"],
        s["spotify_artist"],
        s["spotify_duration_ms"],
        s["cand"],
        strict_seconds=3,
        loose_seconds=10,
    )
    if "want_total_eq" in s:
        assert br.total == s["want_total_eq"], f"{name}: total={br.total}"
    if "want_total_gte" in s:
        assert br.total >= s["want_total_gte"], f"{name}: total={br.total}"
    if "want_total_lt" in s:
        assert br.total < s["want_total_lt"], f"{name}: total={br.total}"
    if "want_total_between" in s:
        lo, hi = s["want_total_between"]
        assert lo <= br.total <= hi, f"{name}: total={br.total} not in [{lo},{hi}]"
    for k in ("want_title_exact", "want_artist_match", "want_duration_strict", "want_duration_loose"):
        if k in s:
            got = getattr(br, k.removeprefix("want_"))
            assert got == s[k], f"{name}: {k}={got}"
    if "want_title_tokens_gt" in s:
        assert br.title_tokens > s["want_title_tokens_gt"], f"{name}: tokens={br.title_tokens}"