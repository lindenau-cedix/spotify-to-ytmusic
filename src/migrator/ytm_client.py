"""YouTube Music client + auth via `ytmusicapi`.

ytmusicapi uses request headers (not OAuth) for auth. The user must extract
their own headers_auth.json from a logged-in YTM browser session and drop it
into the data dir. See README for the exact browser steps.

This module:
- Walks the user through the import (interactive prompt, file path)
- Loads + validates the headers JSON
- Wraps the few ytmusicapi methods we need (search / create_playlist /
  add_playlist_items) so we can swap in mocks in tests and so we can
  attach consistent logging / retry behaviour.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

import ytmusicapi

from .config import get_settings
from .db import upsert_auth
from .logging_setup import get_logger

log = get_logger("ytm")

REQUIRED_HEADER_KEYS = {"cookie", "x-goog-authuser"}


def headers_template_path() -> Path:
    """Where to write the empty header template the user can fill in."""
    return get_settings().data_dir / "headers_auth.json.template"


def write_template() -> Path:
    """Write a starter headers_auth.json so the user knows what to put in."""
    settings = get_settings()
    settings.ensure_data_dir()
    template = {
        "_comment": (
            "Paste your YouTube Music request headers here. Open music.youtube.com, "
            "open DevTools → Network → any POST request, right-click → Copy → Copy "
            "request headers. The required keys are listed below; ytmusicapi also "
            "accepts 'authorization' if you have one. See README.md for full steps."
        ),
        "cookie": "<PASTE YOUR YTM COOKIE>",
        "x-goog-authuser": "0",
        "authorization": "<optional, paste Bearer token if your DevTools shows one>",
    }
    p = headers_template_path()
    p.write_text(json.dumps(template, indent=2), encoding="utf-8")
    return p


def load_headers(path: Path | None = None) -> dict[str, Any]:
    """Load and minimally validate headers_auth.json."""
    settings = get_settings()
    target = path or settings.ytm_headers_path
    if not target.exists():
        raise FileNotFoundError(
            f"No YTM headers at {target}. Run `python -m migrator auth ytm` first."
        )
    raw = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("YTM headers file must be a JSON object at the top level.")
    if not REQUIRED_HEADER_KEYS.issubset(raw):
        missing = REQUIRED_HEADER_KEYS - set(raw)
        raise ValueError(
            f"YTM headers file is missing required keys: {sorted(missing)}. "
            f"See README.md for the exact extraction steps."
        )
    # Reject the placeholder from write_template.
    if "<" in str(raw.get("cookie", "")):
        raise ValueError(
            "YTM headers still contain placeholder text. Replace <PASTE …> with "
            "your real cookie string."
        )
    return raw


def import_headers_from(src: Path) -> Path:
    """Copy a headers file the user provided into the data dir."""
    settings = get_settings()
    settings.ensure_data_dir()
    if not src.exists():
        raise FileNotFoundError(f"Headers file not found: {src}")
    dest = settings.ytm_headers_path
    shutil.copy2(src, dest)
    # Validate so we surface errors immediately.
    load_headers(dest)
    upsert_auth("ytm", True, str(dest))
    log.info("YTM headers imported", extra={"path": str(dest)})
    return dest


def run_auth_flow() -> None:
    """Interactive: tell the user how to extract headers, accept their file."""
    print()
    print("== YouTube Music auth ==")
    print()
    print("ytmusicapi does not use OAuth. You need to copy request headers from a")
    print("logged-in music.youtube.com session into headers_auth.json.")
    print()
    print("Steps:")
    print("  1. Open https://music.youtube.com in Chrome/Firefox and SIGN IN.")
    print("  2. Open DevTools (F12) → Network tab → check 'Preserve log'.")
    print("  3. Click anywhere on the page to trigger a request, or click Library.")
    print("  4. Right-click any request row → Copy → Copy as cURL (bash).")
    print("  5. Use https://curlconverter.com/ or run `ytmusicapi browser` and")
    print("     paste the headers; OR paste them into the template manually:")
    template_path = write_template()
    print(f"     {template_path}")
    print("  6. Save the file as:  .migrator/headers_auth.json")
    print()
    print("When done, type the path (or Enter to accept .migrator/headers_auth.json):")
    raw = input("> ").strip() or str(get_settings().ytm_headers_path)
    import_headers_from(Path(raw).expanduser())


def reset_auth() -> None:
    p = get_settings().ytm_headers_path
    if p.exists():
        p.unlink()
    upsert_auth("ytm", False, "reset")


# ---------- client wrapper ----------

class YTMClient:
    """Thin wrapper around ytmusicapi.YTMusic with logging + retry."""

    def __init__(self, headers: dict[str, Any] | None = None, *, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self._headers = headers if headers is not None else load_headers()
        self._ytm: ytmusicapi.YTMusic | None = None

    @property
    def ytm(self) -> ytmusicapi.YTMusic:
        if self._ytm is None:
            self._ytm = ytmusicapi.YTMusic(auth=self._headers)
        return self._ytm

    def search_songs(self, query: str, limit: int = 5, *, debug: bool = False) -> list[dict[str, Any]]:
        """Top N song results. Returns list of YTM dicts with videoId, title, …

        On exception, logs at warning level (preserves the historical
        silent-[] behaviour used by the matching pipeline) and returns [].
        Pass ``debug=True`` to log at error level with the full traceback
        and re-raise — useful when diagnosing auth or rate-limit issues.
        """
        try:
            res = self.ytm.search(query, filter="songs", limit=limit)
        except Exception as e:  # noqa: BLE001
            if debug:
                log.error(
                    "YTM search failed (debug mode — re-raising)",
                    extra={"query": query, "err": str(e)},
                    exc_info=True,
                )
                raise
            log.warning("YTM search failed", extra={"query": query, "err": str(e)})
            return []
        return list(res or [])

    def search_by_isrc(self, isrc: str) -> list[dict[str, Any]]:
        """Best-effort ISRC lookup. ytmusicapi has no native ISRC field, so we
        try a targeted query and look for ISRC-like hits. Most failures here
        are normal — the text-search fallback covers the rest.
        """
        return self.search_songs(f'"{isrc}"', limit=3)

    def create_playlist(self, title: str, description: str = "",
                        privacy_status: str = "PRIVATE") -> str:
        if self.dry_run:
            log.info("DRY RUN: would create playlist", extra={"title": title})
            return "DRY_RUN_PLAYLIST_ID"
        try:
            res = self.ytm.create_playlist(
                title=title, description=description, privacy_status=privacy_status
            )
        except Exception as e:  # noqa: BLE001
            log.error("create_playlist failed", extra={"title": title, "err": str(e)})
            raise
        pid = res.get("id") or ""
        if not pid:
            raise RuntimeError(f"ytm.create_playlist returned no id: {res!r}")
        return pid

    def add_playlist_items(self, playlist_id: str, video_ids: list[str]) -> list[str]:
        """Add items in batches of `batch_size`. Returns list of video_ids
        that were successfully added (YTM may dedupe)."""
        if self.dry_run:
            log.info(
                "DRY RUN: would add items to playlist",
                extra={"playlist_id": playlist_id, "count": len(video_ids)},
            )
            return list(video_ids)

        settings = get_settings()
        batch = settings.toml.importing.batch_size
        backoff = settings.toml.importing.rate_limit_backoff_seconds
        max_retries = settings.toml.importing.rate_limit_max_retries

        added: list[str] = []
        for i in range(0, len(video_ids), batch):
            chunk = video_ids[i : i + batch]
            for attempt in range(max_retries):
                try:
                    res = self.ytm.add_playlist_items(playlist_id, chunk)
                    break
                except Exception as e:  # noqa: BLE001
                    msg = str(e).lower()
                    if "rate" in msg or "429" in msg:
                        log.warning(
                            "rate-limited by YTM, backing off",
                            extra={"attempt": attempt, "backoff_s": backoff},
                        )
                        time.sleep(backoff)
                        continue
                    if "region" in msg or "not available" in msg:
                        log.warning("region restriction on add", extra={"err": str(e)})
                        res = []
                        break
                    if attempt == max_retries - 1:
                        raise
                    time.sleep(2 * (attempt + 1))
            else:
                log.error("giving up on batch after retries", extra={"chunk": chunk[:5]})
                continue
            # `add_playlist_items` echoes back added items in 'playlistEditResult'
            # but its shape changes; the safest signal is "fewer items added than sent".
            added.extend(chunk)
            if isinstance(res, list) and len(res) < len(chunk):
                log.warning(
                    "YTM accepted fewer items than sent (dedupe or partial block)",
                    extra={"sent": len(chunk), "got": len(res)},
                )
        return added


def ensure_client(*, dry_run: bool = False) -> YTMClient:
    headers = load_headers()
    return YTMClient(headers=headers, dry_run=dry_run)