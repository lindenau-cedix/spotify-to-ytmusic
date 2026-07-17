"""Spotify auth + client.

- OAuth Authorization Code Flow with PKCE (Spotify supports this for public
  clients — we still use a client secret if the user provided one, because
  Spotify Web API requires it for non-PKCE flows and many dashboards still
  ship with a secret).
- Refresh token is stored encrypted (see secrets.py).
- Returns a spotipy.Spotify instance with auto-refreshing credentials.

Usage:
    from migrator.spotify_client import ensure_client
    sp = ensure_client()  # runs interactive auth if no token yet
    playlists = sp.current_user_playlists(limit=50)
"""
from __future__ import annotations

import json
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import spotipy
from spotipy.cache_handler import CacheHandler
from spotipy.oauth2 import SpotifyOAuth

from .config import get_settings
from .db import upsert_auth
from .logging_setup import get_logger
from .secrets import delete_secret, load_secret, store_secret

log = get_logger("spotify")

SPOTIFY_SCOPES = "playlist-read-private playlist-read-collaborative"
REFRESH_TOKEN_ACCOUNT = "spotify-refresh-token"


# ---------- token storage ----------

class KeyringCacheHandler(CacheHandler):
    """Spotipy cache handler backed by our secrets module."""

    def get_cached_token(self) -> dict[str, Any] | None:
        rt = load_secret(REFRESH_TOKEN_ACCOUNT)
        if not rt:
            return None
        # `access_token` is empty by design — we don't store it. Mark the
        # token as already expired so spotipy triggers a refresh on the
        # first API call instead of trying to use the empty bearer.
        return {
            "access_token": "",
            "token_type": "Bearer",
            "expires_in": 3600,
            # 1 second in the past — definitely expired.
            "expires_at": int(time.time()) - 1,
            "refresh_token": rt,
            "scope": SPOTIFY_SCOPES,
        }

    def save_token_to_cache(self, token_info: dict[str, Any]) -> None:
        rt = token_info.get("refresh_token")
        if rt:
            store_secret(REFRESH_TOKEN_ACCOUNT, rt)

    def refresh_access_token(self, refresh_token: str | None = None) -> dict[str, Any] | None:
        return None


# ---------- one-shot local callback server ----------

class _CallbackHandler(BaseHTTPRequestHandler):
    """Serves the Spotify redirect, captures the ?code=…, then shuts down."""

    captured: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        params = parse_qs(url.query)
        _CallbackHandler.captured = {k: v[0] for k, v in params.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body style='font-family:sans-serif;padding:2rem'>"
            b"<h2>Spotify auth complete</h2>"
            b"<p>You can close this tab and return to the migrator.</p>"
            b"</body></html>"
        )

    def log_message(self, *_args: Any) -> None:  # silence default access logs
        return


def _await_local_redirect(port: int, timeout: float = 180.0) -> dict[str, str]:
    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = timeout
    thread = Thread(target=server.handle_request, daemon=True)
    thread.start()
    try:
        thread.join(timeout=timeout)
    finally:
        server.server_close()
    return _CallbackHandler.captured


# ---------- public API ----------

def _build_oauth() -> SpotifyOAuth:
    settings = get_settings()
    client_id = settings.env.spotify_client_id
    client_secret = settings.env.spotify_client_secret
    redirect = settings.env.spotify_redirect_uri
    if not client_id:
        raise RuntimeError(
            "SPOTIFY_CLIENT_ID is not set. Add it to .env (see .env.example) "
            "and re-run `python -m migrator auth spotify`."
        )
    # Spotipy handles PKCE if open_browser=False and we capture the redirect
    # ourselves; otherwise open_browser=True + a localhost listener works too.
    return SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret or None,
        redirect_uri=redirect,
        scope=SPOTIFY_SCOPES,
        cache_handler=KeyringCacheHandler(),
        open_browser=False,
    )


def _port_from_redirect(redirect_uri: str) -> int:
    try:
        return urlparse(redirect_uri).port or 8000
    except ValueError as e:
        raise RuntimeError(f"Invalid SPOTIFY_REDIRECT_URI: {redirect_uri!r} ({e})") from None


def run_auth_flow() -> None:
    """Interactive: open browser, run a tiny local callback server, capture code."""
    settings = get_settings()
    oauth = _build_oauth()
    auth_url = oauth.get_authorize_url()
    port = _port_from_redirect(settings.env.spotify_redirect_uri)
    log.info("opening browser for Spotify auth", extra={"url": auth_url})
    print("\nOpening browser to:\n  ", auth_url, "\n")
    if not webbrowser.open(auth_url):
        print("If your browser did not open, paste this URL manually.")

    print(f"Listening for Spotify redirect on {settings.env.spotify_redirect_uri} …")
    captured = _await_local_redirect(port=port)
    code = captured.get("code")
    if not code:
        upsert_auth("spotify", False, "no code in redirect")
        raise RuntimeError(f"Did not receive a Spotify auth code. Got params: {captured!r}")

    token_info = oauth.get_access_token(code, as_dict=True, check_cache=False)
    # KeyringCacheHandler.save_token_to_cache fires automatically.
    upsert_auth("spotify", True, "ok")
    log.info("Spotify auth complete")


def reset_auth() -> None:
    delete_secret(REFRESH_TOKEN_ACCOUNT)
    upsert_auth("spotify", False, "reset")


def _client_from_refresh_token() -> spotipy.Spotify | None:
    oauth = _build_oauth()
    token_info = oauth.validate_token(oauth.cache_handler.get_cached_token())
    if not token_info:
        return None
    return spotipy.Spotify(auth_manager=oauth)


def ensure_client() -> spotipy.Spotify:
    """Return an authenticated spotipy client, prompting if needed."""
    client = _client_from_refresh_token()
    if client is None:
        run_auth_flow()
        client = _client_from_refresh_token()
    if client is None:
        raise RuntimeError("Spotify auth succeeded but client could not be built. Re-run auth.")
    return client


# ---------- data helpers ----------

def list_playlists(sp: spotipy.Spotify) -> list[dict[str, Any]]:
    """Paginate through current_user_playlists (handles >50)."""
    results: list[dict[str, Any]] = []
    page = sp.current_user_playlists(limit=50)
    while True:
        results.extend(page.get("items") or [])
        if page.get("next"):
            page = sp.next(page)
        else:
            break
    return results


def iter_playlist_tracks(
    sp: spotipy.Spotify, playlist_id: str
) -> tuple[str, list[dict[str, Any]]]:
    """Yield all tracks of a playlist. Returns (snapshot_id, items).

    Skips None entries (e.g. local files Spotify can't resolve) and unwraps
    `track` from `episode` rows where present.
    """
    items: list[dict[str, Any]] = []
    snapshot_id = ""
    # NOTE: no `fields=` mask AND restrict `additional_types` to track only.
    # The default `additional_types=("track","episode")` makes Spotify return
    # 403 on the `/tracks` endpoint for some user-owned playlists in
    # Development Mode. We don't migrate episodes, so just ask for tracks.
    page = sp.playlist_items(playlist_id, limit=100, additional_types=("track",))
    while True:
        snapshot_id = snapshot_id or (page.get("snapshot_id") or "")
        for it in page.get("items") or []:
            track = it.get("track")
            if track is None:
                continue
            # Local files have no id; episodes don't either. Either way they
            # can't be looked up on YTM, so drop them.
            if not track.get("id"):
                continue
            items.append(track)
        if page.get("next"):
            page = sp.next(page)
        else:
            break
    return snapshot_id, items