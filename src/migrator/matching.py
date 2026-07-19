"""Track matching: 3-tier strategy with a deterministic scoring function.

Tier 1: ISRC lookup (best)
Tier 2: text-search fallback with scoring
Tier 3: manual review queue

The scoring function is intentionally pure (no I/O) so it is table-tested.
"""
from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from .config import get_settings
from .db import Matches
from .logging_setup import get_logger
from .ytm_client import YTMClient

log = get_logger("match")


# ---------- normalization & scoring ----------

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+", re.UNICODE)


def _normalize(text: str) -> str:
    """Lowercase, strip diacritics, drop punctuation, collapse whitespace."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def _artist_blob(ytm_candidate: dict[str, Any]) -> str:
    artists = ytm_candidate.get("artists") or []
    if isinstance(artists, list):
        names = [a.get("name", "") if isinstance(a, dict) else str(a) for a in artists]
    else:
        names = [str(artists)]
    return _normalize(" ".join(n for n in names if n))


def _title(ytm_candidate: dict[str, Any]) -> str:
    return _normalize(ytm_candidate.get("title") or "")


def _duration_ms(ytm_candidate: dict[str, Any]) -> int:
    """YTM durations come as either `duration_seconds` or `duration` like '3:42'."""
    if "duration_seconds" in ytm_candidate and ytm_candidate["duration_seconds"] is not None:
        try:
            return int(float(ytm_candidate["duration_seconds"]) * 1000)
        except (TypeError, ValueError):
            pass
    d = ytm_candidate.get("duration")
    if isinstance(d, str) and ":" in d:
        try:
            m, s = d.split(":")
            return (int(m) * 60 + int(s)) * 1000
        except ValueError:
            return 0
    return 0


@dataclass(frozen=True)
class ScoreBreakdown:
    total: float
    title_exact: float
    artist_match: float
    duration_strict: float
    duration_loose: float
    title_tokens: float = 0.0  # partial-credit fallback


def score_candidate(
    spotify_title: str,
    spotify_artist: str,
    spotify_duration_ms: int,
    ytm_candidate: dict[str, Any],
    *,
    strict_seconds: int = 3,
    loose_seconds: int = 10,
) -> ScoreBreakdown:
    """Score one YTM candidate against a Spotify track. Pure function.

    Rules (per spec):
      +0.4  title matches exactly (after normalize)
      +0.3  any artist substring match
      +0.2  duration within ±strict_seconds
      +0.1  duration within ±loose_seconds (and not already +0.2)

    Total cap = 1.0. We also add a small token-overlap credit for the title
    so a candidate whose title is a near-miss (e.g. "Clair de Lune - Edit")
    still beats a totally wrong one — but never enough to flip an obvious miss
    into an accept.
    """
    cand_title = _title(ytm_candidate)
    spot_title = _normalize(spotify_title)
    spot_artist = _normalize(spotify_artist)
    cand_artist = _artist_blob(ytm_candidate)

    title_exact = 0.4 if cand_title and cand_title == spot_title else 0.0

    # Partial title credit — token Jaccard on normalized titles. Capped low.
    title_tokens = 0.0
    if spot_title and cand_title and not title_exact:
        a = set(spot_title.split())
        b = set(cand_title.split())
        if a and b:
            jacc = len(a & b) / len(a | b)
            title_tokens = round(jacc * 0.15, 3)  # max +0.15

    artist_match = 0.3 if (spot_artist and (spot_artist in cand_artist or cand_artist in spot_artist)) else 0.0

    duration_strict = 0.0
    duration_loose = 0.0
    cand_dur = _duration_ms(ytm_candidate)
    if cand_dur and spotify_duration_ms:
        diff_s = abs(cand_dur - spotify_duration_ms) / 1000
        if diff_s <= strict_seconds:
            duration_strict = 0.2
        elif diff_s <= loose_seconds:
            duration_loose = 0.1

    total = title_exact + title_tokens + artist_match + duration_strict + duration_loose
    total = min(total, 1.0)
    return ScoreBreakdown(
        total=round(total, 3),
        title_exact=title_exact,
        artist_match=artist_match,
        duration_strict=duration_strict,
        duration_loose=duration_loose,
        title_tokens=title_tokens,
    )


def classify(score: float, *, accept: float, review: float) -> str:
    if score >= accept:
        return "accepted"
    if score >= review:
        return "review"
    return "skipped"


# ---------- match runner ----------

@dataclass
class MatchRequest:
    spotify_track_id: str
    title: str
    artists: list[str]
    album: str = ""
    duration_ms: int = 0
    isrc: str = ""


@dataclass
class MatchResult:
    request: MatchRequest
    candidates: list[dict[str, Any]] = field(default_factory=list)
    chosen: dict[str, Any] | None = None
    score: float = 0.0
    breakdown: ScoreBreakdown | None = None
    method: str = ""  # "isrc" | "search" | "manual"
    status: str = ""  # accepted | review | skipped


class Matcher:
    """Runs the 3-tier matching flow against a YTMClient."""

    def __init__(
        self,
        client: YTMClient,
        *,
        concurrency: int | None = None,
        top_k: int | None = None,
        accept_threshold: float | None = None,
        review_threshold: float | None = None,
        strict_search: bool = False,
    ) -> None:
        s = get_settings()
        self.client = client
        self.strict_search = strict_search  # if True, surface throttle instead of degrading
        self.concurrency = concurrency or s.concurrency
        self.top_k = top_k or s.toml.matching.search_top_k
        self.accept_threshold = accept_threshold or s.threshold_accept
        self.review_threshold = review_threshold or s.threshold_review
        self.strict = s.toml.matching.duration_tolerance_strict_seconds
        self.loose = s.toml.matching.duration_tolerance_loose_seconds

    async def match_one(self, req: MatchRequest) -> MatchResult:
        candidates: list[dict[str, Any]] = []
        isrc_hit = False
        # Tier 1: ISRC
        if req.isrc:
            isrc_hits = await asyncio.to_thread(self.client.search_by_isrc, req.isrc)
            if isrc_hits and any(c.get("videoId") for c in isrc_hits):
                candidates = [c for c in isrc_hits if c.get("videoId")][: self.top_k]
                isrc_hit = True

        # Tier 2: text fallback (always run if ISRC didn't yield a clean win)
        method = "isrc" if isrc_hit else "search"
        if not candidates:
            query = f"{req.title} {' '.join(req.artists)}".strip()
            search_fn = (
                self.client.search_songs_strict
                if self.strict_search
                else self.client.search_songs
            )
            candidates = await asyncio.to_thread(search_fn, query, self.top_k)
            method = "search"

        # Score & pick.
        best: tuple[float, dict[str, Any] | None, ScoreBreakdown | None] = (0.0, None, None)
        for c in candidates:
            if not c.get("videoId"):
                continue
            spotify_artist = req.artists[0] if req.artists else ""
            br = score_candidate(
                req.title,
                spotify_artist,
                req.duration_ms,
                c,
                strict_seconds=self.strict,
                loose_seconds=self.loose,
            )
            if br.total > best[0]:
                best = (br.total, c, br)

        score, chosen, breakdown = best
        # ISRC hit bonus: we got a result from an ISRC query, so trust it a bit
        # more than text-search alone. +0.15 nudges the realistic "same recording,
        # slightly different title string" case into 'accepted' without making it
        # possible to float a totally unrelated ISRC result past the threshold
        # (we still require some title/artist/duration signal from scoring).
        if isrc_hit and chosen is not None:
            score = min(score + 0.15, 1.0)
        status = classify(score, accept=self.accept_threshold, review=self.review_threshold)
        return MatchResult(
            request=req,
            candidates=candidates,
            chosen=chosen,
            score=score,
            breakdown=breakdown,
            method=method,
            status=status,
        )

    async def match_many(
        self, requests: Iterable[MatchRequest], on_done: Callable[[MatchResult], Awaitable[None]] | None = None
    ) -> list[MatchResult]:
        # A shared lock serialises the actual YTM calls. Concurrency still
        # buys us overlap on I/O the matcher does outside ytmusicapi (mostly
        # scoring), but more importantly it caps the burst rate at "one call
        # at a time" — the only setting that survives contact with YTM's
        # anti-abuse limiter for a fresh session. The limiter trips after a
        # handful of concurrent searches; spacing them out is the single
        # biggest lever for getting through a full playlist.
        lock = asyncio.Lock()
        results: list[MatchResult] = []

        async def _run(req: MatchRequest) -> MatchResult:
            try:
                # Serialise the network call; scoring is already off-thread.
                async with lock:
                    r = await self.match_one(req)
            except Exception as e:  # noqa: BLE001
                log.warning("match error", extra={"track_id": req.spotify_track_id, "err": str(e)})
                r = MatchResult(request=req, method="error", status="skipped")
            if on_done:
                await on_done(r)
            return r

        # Honour the configured concurrency for parallel scoring work, but
        # the YTM call itself is serialised via the lock above. This means
        # raising concurrency above 1 only helps if the matcher gains more
        # non-network work later; for now it's effectively sequential.
        sem = asyncio.Semaphore(self.concurrency)
        async def _bounded(req: MatchRequest) -> MatchResult:
            async with sem:
                return await _run(req)
        results = await asyncio.gather(*[_bounded(r) for r in requests])
        return results


# ---------- persistence helpers ----------

def candidates_to_json(candidates: list[dict[str, Any]]) -> str:
    return json.dumps(candidates, ensure_ascii=False, default=str)


def persist_match(result: MatchResult, playlist_id: str, snapshot_id: str) -> None:
    from sqlmodel import Session, select

    from .db import get_engine, Matches as M  # local import to avoid cycle

    engine = get_engine()
    with Session(engine) as s:
        existing = s.exec(
            select(M).where(
                M.playlist_id == playlist_id,
                M.snapshot_id == snapshot_id,
                M.spotify_track_id == result.request.spotify_track_id,
            )
        ).first()
        breakdown = result.breakdown
        chosen = result.chosen or {}
        row = existing or M(
            playlist_id=playlist_id,
            snapshot_id=snapshot_id,
            spotify_track_id=result.request.spotify_track_id,
        )
        row.method = result.method or row.method or "search"
        row.status = result.status or row.status or "skipped"
        row.score = result.score
        row.candidates_json = candidates_to_json(result.candidates)
        row.chosen_video_id = (chosen.get("videoId") if chosen else None) or row.chosen_video_id
        row.chosen_title = chosen.get("title", "") if chosen else row.chosen_title
        row.chosen_artists = (
            ", ".join(a.get("name", "") for a in chosen.get("artists", []))
            if chosen
            else row.chosen_artists
        )
        row.chosen_duration_ms = _duration_ms(chosen) if chosen else row.chosen_duration_ms
        if breakdown:
            row.note = (
                f"title={breakdown.title_exact} "
                f"artist={breakdown.artist_match} "
                f"dur_strict={breakdown.duration_strict} "
                f"dur_loose={breakdown.duration_loose} "
                f"title_tokens={breakdown.title_tokens}"
            ).strip()
        s.add(row)
        s.commit()


# ---------- manual decision application ----------

def apply_manual_decision(
    *,
    playlist_id: str,
    snapshot_id: str,
    spotify_track_id: str,
    decision: str,  # "accepted" | "skipped"
    chosen: dict[str, Any] | None = None,
) -> None:
    from sqlmodel import Session, select

    from .db import get_engine, Matches as M

    engine = get_engine()
    with Session(engine) as s:
        row = s.exec(
            select(M).where(
                M.playlist_id == playlist_id,
                M.snapshot_id == snapshot_id,
                M.spotify_track_id == spotify_track_id,
            )
        ).first()
        if row is None:
            row = M(
                playlist_id=playlist_id,
                snapshot_id=snapshot_id,
                spotify_track_id=spotify_track_id,
            )
            s.add(row)
        row.method = "manual"
        row.status = decision
        if chosen:
            row.chosen_video_id = chosen.get("videoId")
            row.chosen_title = chosen.get("title", "")
            row.chosen_artists = ", ".join(a.get("name", "") for a in chosen.get("artists", []))
            row.chosen_duration_ms = _duration_ms(chosen)
            row.score = 1.0  # user-confirmed
        s.commit()