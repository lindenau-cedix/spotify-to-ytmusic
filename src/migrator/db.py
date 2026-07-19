"""SQLite persistence via sqlmodel.

Tables:
  Playlists          — Spotify playlist metadata + which YTM playlist (if any) it became
  Tracks             — Spotify tracks (one row per track per snapshot)
  Matches            — YTM match candidate + scoring decision per track
  ImportedTracks     — tracks actually pushed to YTM (for resume)
  AuthState          — non-secret auth markers (e.g. "spotify:ok", "ytm:ok")

We use one DB file per user, defaulting to ./.migrator/migrator.db.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import NamedTuple, Optional

from sqlmodel import Field, Session, SQLModel, create_engine, select

from .config import get_settings


# ---------- models ----------

class Playlists(SQLModel, table=True):
    __tablename__ = "playlists"

    playlist_id: str = Field(primary_key=True)
    snapshot_id: str
    name: str
    description: str = ""
    owner: str = ""
    track_count: int = 0
    is_collaborative: bool = False
    is_public: bool = False
    spotify_url: str = ""
    ytm_playlist_id: Optional[str] = None
    imported_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Tracks(SQLModel, table=True):
    __tablename__ = "tracks"

    # Composite identity (playlist, snapshot, position, spotify_track_id)
    id: Optional[int] = Field(default=None, primary_key=True)
    playlist_id: str = Field(index=True)
    snapshot_id: str
    position: int
    spotify_track_id: str = Field(index=True)
    name: str
    artists: str  # JSON list of strings
    album: str = ""
    duration_ms: int = 0
    isrc: str = ""
    spotify_url: str = ""
    raw_json: str = ""  # full Spotify track payload for debugging


class Matches(SQLModel, table=True):
    __tablename__ = "matches"

    id: Optional[int] = Field(default=None, primary_key=True)
    playlist_id: str = Field(index=True)
    snapshot_id: str
    spotify_track_id: str = Field(index=True)
    # "isrc" | "search" | "manual"
    method: str
    # "accepted" | "review" | "skipped" | "manual_accepted" | "manual_rejected"
    status: str
    score: float = 0.0
    candidates_json: str = "[]"  # JSON list of YTM candidates
    chosen_video_id: Optional[str] = None
    chosen_title: str = ""
    chosen_artists: str = ""
    chosen_duration_ms: int = 0
    note: str = ""
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ImportedTracks(SQLModel, table=True):
    """Tracks actually pushed to a specific YTM playlist. Used for resume."""

    __tablename__ = "imported_tracks"

    id: Optional[int] = Field(default=None, primary_key=True)
    playlist_id: str = Field(index=True)
    ytm_playlist_id: str = Field(index=True)
    video_id: str = Field(index=True)
    spotify_track_id: str
    imported_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AuthState(SQLModel, table=True):
    __tablename__ = "auth_state"

    service: str = Field(primary_key=True)
    ok: bool = False
    detail: str = ""
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------- engine / session ----------

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        settings.ensure_data_dir()
        url = f"sqlite:///{settings.db_path}"
        _engine = create_engine(url, echo=False, connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(_engine)
    return _engine


def get_session() -> Session:
    return Session(get_engine())


def upsert_auth(service: str, ok: bool, detail: str = "") -> None:
    with get_session() as s:
        row = s.get(AuthState, service)
        if row is None:
            row = AuthState(service=service, ok=ok, detail=detail)
            s.add(row)
        else:
            row.ok = ok
            row.detail = detail
            row.updated_at = datetime.now(timezone.utc)
        s.commit()


def auth_ok(service: str) -> bool:
    with get_session() as s:
        row = s.get(AuthState, service)
        return bool(row and row.ok)


def get_playlist(playlist_id: str) -> Optional[Playlists]:
    with get_session() as s:
        return s.get(Playlists, playlist_id)


def list_playlists() -> list[Playlists]:
    with get_session() as s:
        return list(s.exec(select(Playlists).order_by(Playlists.name)).all())


def set_ytm_playlist_id(playlist_id: str, ytm_playlist_id: str) -> None:
    with get_session() as s:
        row = s.get(Playlists, playlist_id)
        if row is not None:
            row.ytm_playlist_id = ytm_playlist_id
            row.updated_at = datetime.now(timezone.utc)
            s.commit()


class AcceptSummary(NamedTuple):
    """Per-playlist outcome of :func:`accept_reviews`."""

    playlist_id: str
    name: str
    accepted: int
    #: `review` rows that were left alone because matching found no candidate
    #: to accept — there's no videoId to import, so flipping them to
    #: `accepted` would silently drop them at import time instead.
    no_candidate: int


def accept_reviews(
    playlist_id: Optional[str] = None,
    *,
    threshold: float = 0.0,
    dry_run: bool = False,
) -> list[AcceptSummary]:
    """Flip `review` matches to `accepted` for one playlist, or for all of them.

    Only rows in the playlist's *current* snapshot are touched, mirroring the
    scoping every other stage uses. A row is only accepted if it carries a
    `chosen_video_id` and its score is at least `threshold`; the rest stay in
    `review` so the web UI can still resolve them by hand.

    Status is set to `accepted` (not `manual_accepted`) to match what the web
    UI's bulk-accept endpoint writes — `import` reads both, but keeping one
    spelling means the review filters in the UI behave the same either way.

    Returns one :class:`AcceptSummary` per playlist that had `review` rows.
    With `dry_run=True` the counts are computed but nothing is written.
    """
    with get_session() as s:
        if playlist_id is not None:
            pl = s.get(Playlists, playlist_id)
            playlists = [pl] if pl is not None else []
        else:
            playlists = list(s.exec(select(Playlists).order_by(Playlists.name)).all())

        summaries: list[AcceptSummary] = []
        for pl in playlists:
            rows = list(
                s.exec(
                    select(Matches).where(
                        Matches.playlist_id == pl.playlist_id,
                        Matches.snapshot_id == pl.snapshot_id,
                        Matches.status == "review",
                    )
                ).all()
            )
            if not rows:
                continue
            eligible = [
                m for m in rows if m.chosen_video_id and m.score >= threshold
            ]
            for m in eligible:
                if not dry_run:
                    m.status = "accepted"
                    m.updated_at = datetime.now(timezone.utc)
            summaries.append(
                AcceptSummary(
                    playlist_id=pl.playlist_id,
                    name=pl.name,
                    accepted=len(eligible),
                    no_candidate=sum(1 for m in rows if not m.chosen_video_id),
                )
            )
        if not dry_run:
            s.commit()
        return summaries


def clear_all_matches() -> int:
    """Wipe every row in ``matches``. Returns the rowcount deleted.

    Used by ``migrator run`` to ensure each invocation starts from a clean
    slate — matching the "start by 0" contract. Reviewer decisions in the web
    UI are deliberately discarded; the matching pass will re-score everything
    fresh, and ``review`` rows from a prior run can be re-resolved in the UI
    after the new match pass lands.
    """
    from sqlmodel import delete as sql_delete

    with get_session() as s:
        result = s.exec(sql_delete(Matches))
        s.commit()
        return result.rowcount or 0


def clear_all_imported_tracks() -> int:
    """Wipe every row in ``imported_tracks``. Returns the rowcount deleted.

    Pairs with :func:`clear_all_matches` so the YTM-side resume guard in
    ``_import_one`` doesn't pretend a previous run already pushed tracks that
    are about to be re-pushed.
    """
    from sqlmodel import delete as sql_delete

    with get_session() as s:
        result = s.exec(sql_delete(ImportedTracks))
        s.commit()
        return result.rowcount or 0


def reset_all_playlist_progress() -> int:
    """Clear ``ytm_playlist_id`` and ``imported_at`` on every playlist row.

    Returns the number of playlists touched. Plays the same role as the two
    wipe helpers above for the playlist-level progress columns surfaced by
    ``migrator status`` (``YTM ID``, ``Imported``).
    """
    with get_session() as s:
        rows = list(s.exec(select(Playlists)).all())
        for r in rows:
            r.ytm_playlist_id = None
            r.imported_at = None
            r.updated_at = datetime.now(timezone.utc)
        s.commit()
        return len(rows)