"""pytest fixtures shared across the test suite.

- `isolated_settings` redirects config paths into tmp_path so tests don't
  touch the user's real .migrator/ directory.
- `fake_ytm_client` is a stand-in for YTMClient so tests don't hit the network.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from migrator import config as cfg
from migrator.ytm_client import YTMClient


@pytest.fixture
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Force all config paths under tmp_path."""
    data_dir = tmp_path / ".migrator"
    data_dir.mkdir()
    monkeypatch.setenv("MIGRATOR_DATA_DIR", str(data_dir))
    monkeypatch.setenv("MIGRATOR_DB_PATH", str(data_dir / "migrator.db"))
    cfg.reset_cache()
    yield cfg.get_settings()


@pytest.fixture(autouse=True)
def _no_browser_cookies(monkeypatch: pytest.MonkeyPatch):
    """Never let the suite touch the real browser cookie store.

    ``YTMClient`` reads the live cookie on construction and periodically during
    a run, so without this every test would decrypt the developer's actual
    Chrome profile — slow, keyring-dependent, and different on CI.
    """
    monkeypatch.setattr(
        "migrator.ytm_client._browser_cookie_or_none",
        lambda *args, **kwargs: None,
        raising=False,
    )
    # Neutralise the periodic refresh too. It sits on every request path, so
    # leaving it live would also make it consume the scripted `time.monotonic`
    # clocks that the pacing tests depend on. Tests that want the real thing
    # capture it at import time — see test_ytm_client._REAL_COOKIE_REFRESH.
    monkeypatch.setattr(
        YTMClient, "_maybe_refresh_browser_cookie",
        lambda self, *, force=False: None,
    )


class FakeYTMClient(YTMClient):
    """Replaces network calls with a scripted list of candidates per query."""

    def __init__(self, scripted: dict[str, list[dict[str, Any]]] | None = None):
        # Skip parent __init__ (we don't need real headers).
        self.dry_run = False
        self._scripted = scripted or {}
        self.calls: list[tuple[str, str, float]] = []  # (method, query, t)
        self.last_add_aborted = False
        self.last_add_rejected = 0
        # What YouTube reports as already in the playlist. Tests exercising the
        # "landed on YTM but never recorded locally" path assign this directly.
        self.live_video_ids: set[str] = set()

    def _record(self, method: str, query: str) -> None:
        import time
        self.calls.append((method, query, time.monotonic()))

    def search_songs(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        self._record("search", query)
        return list(self._scripted.get(query, [])[:limit])

    def search_songs_strict(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        # Default behaviour: identical to search_songs (no throttle to raise on).
        # Subclass for tests that want to simulate throttling.
        self._record("search_strict", query)
        return list(self._scripted.get(query, [])[:limit])

    def search_by_isrc(self, isrc: str) -> list[dict[str, Any]]:
        self._record("isrc", isrc)
        return list(self._scripted.get(f"isrc:{isrc}", []))

    def create_playlist(self, title: str, description: str = "", privacy_status: str = "PRIVATE") -> str:
        self.calls.append(("create_playlist", title))
        return "FAKE_YTM_PLAYLIST_ID"

    def add_playlist_items(self, playlist_id: str, video_ids: list[str]) -> list[str]:
        self.calls.append(("add_playlist_items", f"{playlist_id}:{len(video_ids)}"))
        self.sent_video_ids = list(video_ids)
        return list(video_ids)

    def playlist_video_ids(self, playlist_id: str) -> set[str]:
        self.calls.append(("playlist_video_ids", playlist_id))
        return set(self.live_video_ids)


@pytest.fixture
def fake_ytm_client() -> FakeYTMClient:
    return FakeYTMClient()


def load_fixture(name: str) -> dict[str, Any]:
    p = Path(__file__).parent / "fixtures" / name
    return json.loads(p.read_text(encoding="utf-8"))