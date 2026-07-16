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
from typing import Optional

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