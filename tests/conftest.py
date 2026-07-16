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


class FakeYTMClient(YTMClient):
    """Replaces network calls with a scripted list of candidates per query."""

    def __init__(self, scripted: dict[str, list[dict[str, Any]]] | None = None):
        # Skip parent __init__ (we don't need real headers).
        self.dry_run = False
        self._scripted = scripted or {}
        self.calls: list[tuple[str, str]] = []  # (method, query)

    def search_songs(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        self.calls.append(("search", query))
        return list(self._scripted.get(query, [])[:limit])

    def search_by_isrc(self, isrc: str) -> list[dict[str, Any]]:
        self.calls.append(("isrc", isrc))
        return list(self._scripted.get(f"isrc:{isrc}", []))

    def create_playlist(self, title: str, description: str = "", privacy_status: str = "PRIVATE") -> str:
        self.calls.append(("create_playlist", title))
        return "FAKE_YTM_PLAYLIST_ID"

    def add_playlist_items(self, playlist_id: str, video_ids: list[str]) -> list[str]:
        self.calls.append(("add_playlist_items", f"{playlist_id}:{len(video_ids)}"))
        return list(video_ids)


@pytest.fixture
def fake_ytm_client() -> FakeYTMClient:
    return FakeYTMClient()


def load_fixture(name: str) -> dict[str, Any]:
    p = Path(__file__).parent / "fixtures" / name
    return json.loads(p.read_text(encoding="utf-8"))