"""FastAPI web UI: Jinja2 + htmx + Tailwind (CDN).

Pages:
  /                dashboard
  /review          review queue (one row per track)
  /setup           first-run wizard if Spotify or YTM auth missing
  /auth/spotify    kick off Spotify OAuth (opens browser, runs callback server)
  /auth/ytm        show YTM header extraction instructions
  /playlists/{id}  one playlist's overview
  /playlists/{id}/import  POST to push approved matches
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import PROJECT_ROOT, get_settings
from .db import (
    AuthState,
    Matches,
    Playlists,
    Tracks,
    auth_ok,
    get_session,
    list_playlists,
    set_ytm_playlist_id,
    upsert_auth,
)
from .logging_setup import get_logger
from .matching import Matcher, apply_manual_decision
from .spotify_client import run_auth_flow as spotify_auth_flow
from .ytm_client import (
    YTMClient,
    ensure_client as ensure_ytm_client,
    import_headers_from,
    load_headers,
    write_template,
)

log = get_logger("web")

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def create_app() -> FastAPI:
    app = FastAPI(title="spotify-to-ytmusic", docs_url=None, redoc_url=None)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        sp = auth_ok("spotify")
        ytm = auth_ok("ytm")
        pls = list_playlists() if (sp and ytm) else []
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "spotify_ok": sp,
                "ytm_ok": ytm,
                "playlists": pls,
                "settings": get_settings(),
            },
        )

    @app.get("/setup", response_class=HTMLResponse)
    async def setup(request: Request) -> HTMLResponse:
        sp = auth_ok("spotify")
        ytm = auth_ok("ytm")
        return templates.TemplateResponse(
            "setup.html", {"request": request, "spotify_ok": sp, "ytm_ok": ytm}
        )

    @app.post("/auth/ytm/upload")
    async def upload_ytm_headers(request: Request) -> RedirectResponse:
        form = await request.form()
        f = form.get("file")
        if not f or not hasattr(f, "filename"):
            raise HTTPException(400, "no file uploaded")
        path = get_settings().ytm_headers_path
        path.parent.mkdir(parents=True, exist_ok=True)
        body = await f.read()  # type: ignore[attr-defined]
        path.write_bytes(body)
        try:
            load_headers(path)
        except Exception as e:  # noqa: BLE001
            path.unlink(missing_ok=True)
            upsert_auth("ytm", False, str(e))
            raise HTTPException(400, f"Invalid headers file: {e}") from None
        upsert_auth("ytm", True, str(path))
        return RedirectResponse("/", status_code=303)

    @app.get("/auth/ytm/template")
    async def get_ytm_template() -> dict[str, str]:
        p = write_template()
        return {"path": str(p)}

    @app.get("/review", response_class=HTMLResponse)
    async def review(
        request: Request,
        playlist: str | None = None,
        status: str = "review",
    ) -> HTMLResponse:
        pls = list_playlists()
        if not pls:
            return RedirectResponse("/", status_code=303)
        chosen = playlist or pls[0].playlist_id
        with get_session() as s:
            pl = s.get(Playlists, chosen)
            if pl is None:
                raise HTTPException(404, "playlist not found")
            q = (
                s.query(Matches, Tracks)
                .join(
                    Tracks,
                    (Tracks.playlist_id == Matches.playlist_id)
                    & (Tracks.snapshot_id == Matches.snapshot_id)
                    & (Tracks.spotify_track_id == Matches.spotify_track_id),
                )
                .filter(Matches.playlist_id == chosen, Matches.snapshot_id == pl.snapshot_id)
            )
            if status in {"review", "accepted", "skipped", "all"}:
                if status != "all":
                    q = q.filter(Matches.status == status)
            rows = q.order_by(Tracks.position).all()

        rendered = [
            {
                "match_id": m.id,
                "position": t.position,
                "spotify_track_id": t.spotify_track_id,
                "title": t.name,
                "artists": json.loads(t.artists or "[]"),
                "album": t.album,
                "duration_ms": t.duration_ms,
                "isrc": t.isrc,
                "spotify_url": t.spotify_url,
                "status": m.status,
                "score": m.score,
                "method": m.method,
                "chosen_video_id": m.chosen_video_id,
                "chosen_title": m.chosen_title,
                "chosen_artists": m.chosen_artists,
                "candidates": json.loads(m.candidates_json or "[]"),
                "note": m.note,
            }
            for m, t in rows
        ]
        return templates.TemplateResponse(
            "review.html",
            {
                "request": request,
                "playlist": pl,
                "playlists": pls,
                "rows": rendered,
                "filter": status,
            },
        )

    @app.post("/review/{playlist_id}/{track_id}/accept")
    async def review_accept(
        playlist_id: str, track_id: str, video_id: str = Form(...), video_title: str = Form(""),
        video_artists: str = Form(""), video_duration_ms: int = Form(0),
    ) -> dict[str, Any]:
        chosen = {
            "videoId": video_id,
            "title": video_title,
            "artists": [{"name": a.strip()} for a in video_artists.split(",") if a.strip()],
            "duration_ms": video_duration_ms,
        }
        pl = None
        with get_session() as s:
            pl = s.get(Playlists, playlist_id)
        if pl is None:
            raise HTTPException(404, "playlist not found")
        apply_manual_decision(
            playlist_id=playlist_id,
            snapshot_id=pl.snapshot_id,
            spotify_track_id=track_id,
            decision="accepted",
            chosen=chosen,
        )
        return {"ok": True}

    @app.post("/review/{playlist_id}/{track_id}/skip")
    async def review_skip(playlist_id: str, track_id: str) -> dict[str, Any]:
        pl = None
        with get_session() as s:
            pl = s.get(Playlists, playlist_id)
        if pl is None:
            raise HTTPException(404, "playlist not found")
        apply_manual_decision(
            playlist_id=playlist_id,
            snapshot_id=pl.snapshot_id,
            spotify_track_id=track_id,
            decision="skipped",
        )
        return {"ok": True}

    @app.post("/review/{playlist_id}/{track_id}/search")
    async def review_search(
        playlist_id: str, track_id: str, query: str = Form(...)
    ) -> dict[str, Any]:
        """Manual YTM search from the review UI."""
        if not auth_ok("ytm"):
            raise HTTPException(400, "YTM not authenticated")
        client = ensure_ytm_client()
        results = client.search_songs(query, limit=8)
        return {"results": results}

    @app.post("/review/{playlist_id}/bulk-accept")
    async def review_bulk_accept(
        playlist_id: str, threshold: float = Form(0.85)
    ) -> dict[str, Any]:
        """Accept any match currently in 'review' status above threshold."""
        pl = None
        with get_session() as s:
            pl = s.get(Playlists, playlist_id)
            if pl is None:
                raise HTTPException(404, "playlist not found")
            rows = (
                s.query(Matches)
                .filter(
                    Matches.playlist_id == playlist_id,
                    Matches.snapshot_id == pl.snapshot_id,
                    Matches.status == "review",
                    Matches.score >= threshold,
                )
                .all()
            )
            for m in rows:
                m.status = "accepted"
            s.commit()
            n = len(rows)
        return {"ok": True, "accepted": n}

    @app.post("/playlists/{playlist_id}/run-matching")
    async def run_matching(playlist_id: str) -> dict[str, Any]:
        if not auth_ok("ytm"):
            raise HTTPException(400, "YTM not authenticated")
        client = ensure_ytm_client()
        matcher = Matcher(client)
        from sqlmodel import select

        from .matching import MatchRequest, persist_match

        with get_session() as s:
            pl = s.get(Playlists, playlist_id)
            if pl is None:
                raise HTTPException(404, "playlist not found")
            tracks = s.exec(
                select(Tracks)
                .where(Tracks.playlist_id == playlist_id, Tracks.snapshot_id == pl.snapshot_id)
                .order_by(Tracks.position)
            ).all()
        requests = [
            MatchRequest(
                spotify_track_id=t.spotify_track_id,
                title=t.name,
                artists=json.loads(t.artists or "[]"),
                album=t.album,
                duration_ms=t.duration_ms,
                isrc=t.isrc,
            )
            for t in tracks
        ]
        results = await matcher.match_many(requests)
        for r in results:
            persist_match(r, playlist_id=playlist_id, snapshot_id=pl.snapshot_id)
        return {"ok": True, "count": len(results)}

    @app.post("/playlists/{playlist_id}/import")
    async def do_import(playlist_id: str) -> dict[str, Any]:
        # Defer to CLI implementation by invoking it programmatically.
        from .cli import import_cmd
        import typer

        try:
            import_cmd(playlist_id=playlist_id, dry_run=False, yes=True)
        except SystemExit as e:
            if e.code not in (None, 0):
                raise HTTPException(400, "import failed") from None
        return {"ok": True}

    return app


app = create_app()