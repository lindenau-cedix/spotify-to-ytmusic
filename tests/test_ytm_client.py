"""Tests for YTMClient wrapper, focused on return-type handling.

ytmusicapi returns ``Union[str, dict]`` from ``create_playlist`` — a plain
string (the playlist id) on success, or the full response dict on error.
An earlier version of this wrapper assumed ``dict`` unconditionally and
crashed on every real ``run`` with::

    AttributeError: 'str' object has no attribute 'get'

These tests pin both branches so the bug does not regress.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
import requests
from requests.structures import CaseInsensitiveDict

from migrator.ytm_client import YTMClient, YTMNonJSONResponseError


def _client_with_ytm(ytm_mock: object) -> YTMClient:
    """Build a YTMClient that skips header loading and uses a mock ``ytm``."""
    # Bypass __init__ so we don't need real YTM headers.
    client = YTMClient.__new__(YTMClient)
    client.dry_run = False
    client._headers = {}
    client._ytm = ytm_mock
    client._last_create_monotonic = None
    client._last_headers_write = None
    client._last_cookie_read = 0.0
    return client


def test_create_playlist_accepts_string_return() -> None:
    """ytmusicapi >= ~1.10 returns the playlist id as a plain string."""
    ytm = MagicMock()
    ytm.create_playlist.return_value = "PL_REAL_PLAYLIST_ID_123"
    client = _client_with_ytm(ytm)

    pid = client.create_playlist(title="My Mix", description="from Spotify")

    assert pid == "PL_REAL_PLAYLIST_ID_123"
    ytm.create_playlist.assert_called_once_with(
        title="My Mix", description="from Spotify", privacy_status="PRIVATE"
    )


def test_create_playlist_accepts_dict_return() -> None:
    """Older ytmusicapi versions returned ``{"id": "..."}`` — keep that path."""
    ytm = MagicMock()
    ytm.create_playlist.return_value = {"id": "PL_LEGACY_PLAYLIST_ID_456"}
    client = _client_with_ytm(ytm)

    pid = client.create_playlist(title="Legacy")

    assert pid == "PL_LEGACY_PLAYLIST_ID_456"


def test_create_playlist_dict_without_id_raises() -> None:
    """A dict response with no 'id' key is an error, not silent empty string."""
    ytm = MagicMock()
    ytm.create_playlist.return_value = {"status": "ERROR", "message": "boom"}
    client = _client_with_ytm(ytm)

    with pytest.raises(RuntimeError, match="returned no id"):
        client.create_playlist(title="oops")


def test_create_playlist_empty_string_raises() -> None:
    """An empty string from the API is also treated as 'no id'."""
    ytm = MagicMock()
    ytm.create_playlist.return_value = ""
    client = _client_with_ytm(ytm)

    with pytest.raises(RuntimeError, match="returned no id"):
        client.create_playlist(title="oops")


def test_create_playlist_dry_run_skips_api() -> None:
    """Dry-run mode must never hit the API and must return a sentinel id."""
    client = YTMClient.__new__(YTMClient)
    client.dry_run = True
    client._headers = {}
    client._ytm = MagicMock()

    pid = client.create_playlist(title="Anything")

    assert pid == "DRY_RUN_PLAYLIST_ID"
    client._ytm.create_playlist.assert_not_called()


# ---------- retry + pacing (rate-limit hardening) ----------

from ytmusicapi.exceptions import YTMusicServerError


def _rate_limit_error() -> YTMusicServerError:
    return YTMusicServerError("Server returned HTTP 429: Too Many Requests.")


def _signed_in_error() -> YTMusicServerError:
    return YTMusicServerError(
        "Server returned HTTP 401: Unauthorized.\nYou must be signed in to perform this operation."
    )


def test_search_songs_retries_then_succeeds(isolated_settings, monkeypatch) -> None:
    """A transient throttle on search is retried with growing backoff, then wins."""
    sleeps: list[float] = []
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: sleeps.append(s))

    ytm = MagicMock()
    ytm.search.side_effect = [_rate_limit_error(), [{"videoId": "abc", "title": "Song"}]]
    client = _client_with_ytm(ytm)

    res = client.search_songs("some track", limit=5)

    assert res == [{"videoId": "abc", "title": "Song"}]
    assert ytm.search.call_count == 2
    assert len(sleeps) == 1  # one backoff before the retry that succeeded
    assert sleeps[0] == pytest.approx(3.0)  # backoff * 2**0


def test_search_songs_exhausts_retries_returns_empty(isolated_settings, monkeypatch) -> None:
    """When search keeps throttling, degrade silently to [] (never raise)."""
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: None)

    ytm = MagicMock()
    ytm.search.side_effect = _rate_limit_error()  # always throttled
    client = _client_with_ytm(ytm)

    res = client.search_songs("doomed query")

    assert res == []
    assert ytm.search.call_count == 6  # search_retry_max_retries default


def test_search_songs_debug_reraises(isolated_settings, monkeypatch) -> None:
    """debug=True surfaces the exhausted error instead of swallowing it."""
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: None)

    ytm = MagicMock()
    ytm.search.side_effect = _rate_limit_error()
    client = _client_with_ytm(ytm)

    with pytest.raises(YTMusicServerError):
        client.search_songs("q", debug=True)


def test_create_playlist_retries_on_401_then_succeeds(isolated_settings, monkeypatch) -> None:
    """The 'You must be signed in' 401 is a throttle signature — retry it."""
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: None)
    # Neutralize pacing so this test only exercises retry, not the interval wait.
    monkeypatch.setattr("migrator.ytm_client.time.monotonic", lambda: 0.0)

    ytm = MagicMock()
    ytm.create_playlist.side_effect = [_signed_in_error(), "PL_OK_123"]
    client = _client_with_ytm(ytm)

    pid = client.create_playlist(title="Throttled Mix")

    assert pid == "PL_OK_123"
    assert ytm.create_playlist.call_count == 2


def test_create_playlist_exhausts_retries_reraises(isolated_settings, monkeypatch) -> None:
    """If create keeps 401ing, the import must fail loudly (re-raise)."""
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: None)
    monkeypatch.setattr("migrator.ytm_client.time.monotonic", lambda: 0.0)

    ytm = MagicMock()
    ytm.create_playlist.side_effect = _signed_in_error()
    client = _client_with_ytm(ytm)

    with pytest.raises(YTMusicServerError):
        client.create_playlist(title="Doomed")
    assert ytm.create_playlist.call_count == 5  # rate_limit_max_retries default


def test_create_playlist_paces_successive_creations(isolated_settings, monkeypatch) -> None:
    """The second create waits out the remainder of create_min_interval_seconds."""
    sleeps: list[float] = []
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: sleeps.append(s))
    # Clock is consumed as: A's finally-stamp (100.0), B's pacing check
    # (100.5 → 0.5s elapsed), B's finally-stamp (100.5).
    clock = iter([100.0, 100.5, 100.5])
    monkeypatch.setattr("migrator.ytm_client.time.monotonic", lambda: next(clock))

    ytm = MagicMock()
    ytm.create_playlist.side_effect = ["PL_A", "PL_B"]
    client = _client_with_ytm(ytm)

    client.create_playlist(title="A")  # no prior create → no pacing sleep
    client.create_playlist(title="B")  # 0.5s elapsed → sleep ~1.5s

    assert sleeps == [pytest.approx(1.5)]


def _non_json_error() -> json.JSONDecodeError:
    """What ytmusicapi raises when YTM answers with an HTML rate-limit page:
    it json.loads() the body before checking the HTTP status code."""
    return json.JSONDecodeError("Expecting value", "<html>Too Many Requests</html>", 0)


def test_create_playlist_retries_on_non_json_response_then_succeeds(
    isolated_settings, monkeypatch
) -> None:
    """An HTML/empty throttle page (JSONDecodeError) is retried like a 429."""
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: None)
    monkeypatch.setattr("migrator.ytm_client.time.monotonic", lambda: 0.0)

    ytm = MagicMock()
    ytm.create_playlist.side_effect = [_non_json_error(), "PL_OK_789"]
    client = _client_with_ytm(ytm)

    pid = client.create_playlist(title="Throttled HTML Mix")

    assert pid == "PL_OK_789"
    assert ytm.create_playlist.call_count == 2


def test_create_playlist_non_json_exhaustion_raises_actionable_error(
    isolated_settings, monkeypatch
) -> None:
    """Persistent non-JSON responses surface as YTMNonJSONResponseError with a
    hint about rate limiting, not a cryptic 'Expecting value' JSONDecodeError."""
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: None)
    monkeypatch.setattr("migrator.ytm_client.time.monotonic", lambda: 0.0)

    ytm = MagicMock()
    ytm.create_playlist.side_effect = _non_json_error()
    client = _client_with_ytm(ytm)

    with pytest.raises(YTMNonJSONResponseError, match="rate limiting"):
        client.create_playlist(title="Doomed HTML")
    assert ytm.create_playlist.call_count == 5  # rate_limit_max_retries default


def test_search_songs_swallows_non_json_after_retries(isolated_settings, monkeypatch) -> None:
    """Search keeps its silent-degrade contract for non-JSON throttle pages."""
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: None)

    ytm = MagicMock()
    ytm.search.side_effect = _non_json_error()
    client = _client_with_ytm(ytm)

    res = client.search_songs("doomed query")

    assert res == []
    assert ytm.search.call_count == 6  # retried as a throttle, then degraded


def test_search_songs_strict_raises_on_throttle_exhaustion(
    isolated_settings, monkeypatch
) -> None:
    """The strict variant surfaces throttle so the caller can act on it
    instead of silently marking every track as skipped."""
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: None)
    monkeypatch.setattr("migrator.ytm_client.time.monotonic", lambda: 0.0)

    ytm = MagicMock()
    ytm.search.side_effect = _non_json_error()
    client = _client_with_ytm(ytm)

    with pytest.raises(YTMNonJSONResponseError, match="rate limiting"):
        client.search_songs_strict("throttled query")
    assert ytm.search.call_count == 6


def test_search_songs_strict_returns_empty_on_no_results(
    isolated_settings, monkeypatch
) -> None:
    """A clean empty result (no throttle) is still [], not an error."""
    ytm = MagicMock()
    ytm.search.return_value = []
    client = _client_with_ytm(ytm)

    assert client.search_songs_strict("nothing here") == []


def test_add_playlist_items_non_json_exhaustion_skips_batch(
    isolated_settings, monkeypatch
) -> None:
    """A batch that keeps hitting non-JSON throttle pages is skipped (like an
    exhausted 429), letting the rest of the import continue."""
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: None)

    ytm = MagicMock()
    ytm.add_playlist_items.side_effect = _non_json_error()
    client = _client_with_ytm(ytm)

    added = client.add_playlist_items("PL_X", ["vid1", "vid2"])

    assert added == []  # batch skipped, no crash
    assert ytm.add_playlist_items.call_count == 5


def test_create_playlist_recovers_on_401_via_visitor_id_refresh(
    isolated_settings, monkeypatch
) -> None:
    """The real-world 'works for 1-2 then 401s' pattern: a stale X-Goog-Visitor-Id
    causes a 401 'must be signed in' even though the cookie is still valid.
    Refreshing the visitor id once (without any backoff sleep) lets the next
    call succeed. This is what stops users needing a full reauth mid-run."""
    sleeps: list[float] = []
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr("migrator.ytm_client.time.monotonic", lambda: 0.0)

    # Real-ish YTMusic: property descriptor for base_headers (cached_property
    # semantics), MagicMock for create_playlist.
    class _YTM:
        def __init__(self) -> None:
            self._deleted = False
            self.create_playlist = MagicMock(
                side_effect=[_signed_in_error(), "PL_OK_RECOVERED"]
            )

        @property
        def base_headers(self):
            return {"X-Goog-Visitor-Id": "stale_v1"}

        @base_headers.deleter
        def base_headers(self) -> None:
            self._deleted = True

    fake = _YTM()
    client = _client_with_ytm(fake)

    pid = client.create_playlist(title="After session drift")

    assert pid == "PL_OK_RECOVERED"
    assert fake.create_playlist.call_count == 2
    assert fake._deleted is True  # session refreshed exactly once
    assert sleeps == []  # recovery path does NOT sleep


def test_create_playlist_recovery_exhausts_falls_back_to_backoff(
    isolated_settings, monkeypatch
) -> None:
    """If the visitor-id refresh doesn't help (real auth failure), the loop
    falls through to the normal throttle backoff and eventually re-raises.
    No infinite retry, no silent swallowing."""
    sleeps: list[float] = []
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr("migrator.ytm_client.time.monotonic", lambda: 0.0)

    class _YTM:
        def __init__(self) -> None:
            self.create_playlist = MagicMock(side_effect=_signed_in_error())

        @property
        def base_headers(self):
            return {"X-Goog-Visitor-Id": "stale_v1"}

        @base_headers.deleter
        def base_headers(self) -> None:
            pass  # refresh is a no-op, simulating a still-broken session

    fake = _YTM()
    client = _client_with_ytm(fake)

    with pytest.raises(YTMusicServerError):
        client.create_playlist(title="Permanently broken")
    # Attempt 0: 401 → refresh (no sleep) → continue.
    # Attempts 1-3: 401 → not refresh-eligible anymore → sleep 30*2**n → retry.
    # Attempt 4: 401 → last attempt → break and re-raise.
    assert fake.create_playlist.call_count == 5
    # create_playlist passes no max_backoff_seconds, so the ramp is uncapped.
    assert sleeps == [pytest.approx(60.0), pytest.approx(120.0), pytest.approx(240.0)]


def test_add_playlist_items_does_not_recover_on_401(
    isolated_settings, monkeypatch
) -> None:
    """A 401 on add gets exactly one visitor-id refresh, then surfaces.

    Sustained throttling makes YouTube answer a playlist edit with a spurious
    "You are not authorized to edit this playlist", which the visitor-id
    refresh clears — losing a whole playlist to that is the worse trade. But
    401 stays out of ``_IMPORT_RETRY``, so a *genuine* ownership error costs
    one extra request rather than a full backoff ladder."""
    sleeps: list[float] = []
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: sleeps.append(s))

    ytm = MagicMock()
    ytm.add_playlist_items.side_effect = _signed_in_error()
    client = _client_with_ytm(ytm)

    with pytest.raises(YTMusicServerError):
        client.add_playlist_items("PL_X", ["vid1"])
    assert ytm.add_playlist_items.call_count == 2  # one refresh + one retry
    assert sleeps == []  # the refresh retries immediately, never backs off


def test_add_playlist_items_recovers_from_spurious_401(
    isolated_settings, monkeypatch
) -> None:
    """The real-world case this exists for: throttling provokes a bogus
    "not authorized to edit this playlist", the visitor-id refresh clears it,
    and the playlist completes instead of aborting."""
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: None)

    ytm = MagicMock()
    ytm.add_playlist_items.side_effect = [
        _signed_in_error(),
        _add_success(["vid1", "vid2"]),
    ]
    client = _client_with_ytm(ytm)

    added = client.add_playlist_items("PL_X", ["vid1", "vid2"])

    assert added == ["vid1", "vid2"]
    assert client.last_add_aborted is False


def test_refresh_session_drops_cached_visitor_id(isolated_settings) -> None:
    """_refresh_session must clear the cached_property value so the next
    access to base_headers re-fetches from ytcfg."""
    # Real object with a property descriptor — MagicMock.__delattr__ shadowing
    # makes the descriptor protocol unreachable on mocks.
    class _FakeYTM:
        def __init__(self) -> None:
            self._deleted: list[str] = []
            self._read_count = 0

        @property
        def base_headers(self):
            self._read_count += 1
            return {"X-Goog-Visitor-Id": "stale_v1"}

        @base_headers.deleter
        def base_headers(self) -> None:
            self._deleted.append("base_headers")

    fake = _FakeYTM()
    client = _client_with_ytm(fake)

    client._refresh_session()

    assert fake._deleted == ["base_headers"]


def test_refresh_session_noop_when_ytm_unbuilt(isolated_settings) -> None:
    """If the YTMusic instance was never built, refresh is a no-op (the next
    .ytm access will build it fresh from the current headers file)."""
    client = _client_with_ytm(MagicMock())
    client._ytm = None
    client._refresh_session()  # must not raise


# ---------- add_playlist_items result parsing ----------
#
# ytmusicapi returns a dict from add_playlist_items, never a list. The old
# `isinstance(res, list)` guard was therefore dead code and every batch was
# recorded as imported regardless of what YouTube accepted — phantom rows that
# permanently suppress re-import. These pin the real shapes.


def _add_success(video_ids: list[str]) -> dict:
    """The real success shape from ytmusicapi.mixins.playlists:363-370."""
    return {
        "status": "STATUS_SUCCEEDED",
        "playlistEditResults": [
            {"videoId": v, "setVideoId": f"set_{v}"} for v in video_ids
        ],
    }


def test_add_playlist_items_records_only_confirmed_ids(isolated_settings) -> None:
    """YTM confirmed 1 of 2 — the dropped one must NOT be recorded as imported."""
    ytm = MagicMock()
    ytm.add_playlist_items.return_value = _add_success(["vid1"])
    client = _client_with_ytm(ytm)

    added = client.add_playlist_items("PL_X", ["vid1", "vid2"])

    assert added == ["vid1"]


def test_add_playlist_items_success_without_detail_assumes_all(isolated_settings) -> None:
    """A SUCCEEDED status with no per-video mapping keeps the optimistic
    contract — we have nothing to contradict it."""
    ytm = MagicMock()
    ytm.add_playlist_items.return_value = {"status": "STATUS_SUCCEEDED"}
    client = _client_with_ytm(ytm)

    assert client.add_playlist_items("PL_X", ["vid1", "vid2"]) == ["vid1", "vid2"]


def test_add_playlist_items_failure_dict_records_nothing(
    isolated_settings, monkeypatch
) -> None:
    """On failure ytmusicapi returns the raw response dict (no SUCCEEDED
    status). Nothing landed, so nothing may be recorded.

    A refusal this total bisects down to single ids before giving up, so each
    one is reported individually rather than as one opaque failed batch.
    """
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: None)
    ytm = MagicMock()
    ytm.add_playlist_items.return_value = {"responseContext": {}}
    client = _client_with_ytm(ytm)

    assert client.add_playlist_items("PL_X", ["vid1", "vid2"]) == []
    assert sorted(client.rejected_video_ids) == ["vid1", "vid2"]


def test_add_playlist_items_region_block_records_nothing(isolated_settings) -> None:
    """Region-restricted tracks never landed; recording them would subtract
    them from every future run and they'd never be retried."""
    ytm = MagicMock()
    ytm.add_playlist_items.side_effect = YTMusicServerError(
        "Server returned HTTP 400: Bad Request.\nThis video is not available in your region."
    )
    client = _client_with_ytm(ytm)

    added = client.add_playlist_items("PL_X", ["vid1", "vid2"])

    assert added == []
    assert client.last_add_aborted is False  # region block is not an abort


def test_add_playlist_items_flags_abort_on_throttle_exhaustion(
    isolated_settings, monkeypatch
) -> None:
    """Stopping early after throttling must be visible to the caller, so it can
    say 're-run to resume' instead of reporting a clean success."""
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: None)

    ytm = MagicMock()
    ytm.add_playlist_items.side_effect = _rate_limit_error()
    client = _client_with_ytm(ytm)

    added = client.add_playlist_items("PL_X", ["vid1"])

    assert added == []
    assert client.last_add_aborted is True


# ---------- transient transport / 409 handling ----------
#
# Both regressions below come from the same real run: back-to-back batch adds
# pushed YouTube's limiter into dropping the TCP connection and answering
# "HTTP 409: Conflict", neither of which the old retry set matched. They
# escaped `_call_with_retry` as genuine errors and killed the playlist.


def _conflict_error() -> YTMusicServerError:
    return YTMusicServerError(
        "Server returned HTTP 409: Conflict.\nSorry, something went wrong."
    )


def test_add_playlist_items_retries_dropped_connection(
    isolated_settings, monkeypatch
) -> None:
    """`ConnectionResetError: [Errno 104]` reaches us as a requests
    ConnectionError. It is YouTube hanging up on us, not a bad request."""
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: None)

    ytm = MagicMock()
    ytm.add_playlist_items.side_effect = [
        requests.exceptions.ConnectionError(
            "('Connection aborted.', ConnectionResetError(104, 'Connection reset by peer'))"
        ),
        _add_success(["vid1", "vid2"]),
    ]
    client = _client_with_ytm(ytm)

    added = client.add_playlist_items("PL_X", ["vid1", "vid2"])

    assert added == ["vid1", "vid2"]
    assert client.last_add_aborted is False
    assert ytm.add_playlist_items.call_count == 2


def test_add_playlist_items_retries_conflict_then_succeeds(
    isolated_settings, monkeypatch
) -> None:
    """A 409 that clears on retry never needs the splitting path."""
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: None)

    ytm = MagicMock()
    ytm.add_playlist_items.side_effect = [
        _conflict_error(),
        _add_success(["vid1", "vid2"]),
    ]
    client = _client_with_ytm(ytm)

    assert client.add_playlist_items("PL_X", ["vid1", "vid2"]) == ["vid1", "vid2"]
    assert client.last_add_aborted is False


def test_add_playlist_items_splits_batch_that_keeps_conflicting(
    isolated_settings, monkeypatch
) -> None:
    """A 409 that survives the backoff loop is about this batch, so halve it.
    Both halves land, and the playlist completes instead of aborting."""
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: None)
    vids = [f"vid{i}" for i in range(24)]

    def _add(_pid: str, chunk: list[str]):
        if len(chunk) > 12:
            raise _conflict_error()
        return _add_success(chunk)

    ytm = MagicMock()
    ytm.add_playlist_items.side_effect = _add
    client = _client_with_ytm(ytm)

    added = client.add_playlist_items("PL_X", vids)

    assert added == vids  # split halves preserve order
    assert client.last_add_aborted is False


def test_add_playlist_items_stops_splitting_at_min_batch_size(
    isolated_settings, monkeypatch
) -> None:
    """The split must bottom out. Without a floor, a permanently stuck
    playlist degrades into one request per track, each with a full retry
    budget — hours of backoff instead of a resumable abort."""
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: None)

    ytm = MagicMock()
    ytm.add_playlist_items.side_effect = _conflict_error()
    client = _client_with_ytm(ytm)

    added = client.add_playlist_items("PL_X", [f"vid{i}" for i in range(8)])

    assert added == []
    assert client.last_add_aborted is True  # caller reports "re-run to resume"


def test_add_playlist_items_paces_successive_batches(
    isolated_settings, monkeypatch
) -> None:
    """Spacing batches is what *prevents* the throttle; the retry logic only
    cleans up after it. No sleep before the first batch."""
    sleeps: list[float] = []
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: sleeps.append(s))
    interval = isolated_settings.toml.importing.batch_interval_seconds
    batch = isolated_settings.toml.importing.batch_size

    ytm = MagicMock()
    ytm.add_playlist_items.side_effect = lambda _pid, chunk: _add_success(chunk)
    client = _client_with_ytm(ytm)

    client.add_playlist_items("PL_X", [f"vid{i}" for i in range(batch * 3)])

    assert ytm.add_playlist_items.call_count == 3
    assert sleeps == [pytest.approx(interval)] * 2


def test_add_playlist_items_clears_abort_flag_per_call(isolated_settings) -> None:
    """A stale flag from a previous playlist must not leak into the next one."""
    ytm = MagicMock()
    ytm.add_playlist_items.return_value = _add_success(["vid1"])
    client = _client_with_ytm(ytm)
    client.last_add_aborted = True

    client.add_playlist_items("PL_X", ["vid1"])

    assert client.last_add_aborted is False


# ---------- live browser cookie ----------
#
# A hand-exported cookie stops authenticating ~15-20 min after export while
# the browser it came from stays signed in: Google rotates the session
# continuation cookies via accounts.google.com, and the youtubei endpoints we
# call never send those rotations back, so there is nothing in our own response
# jar to adopt. Reading the browser's store — and re-reading it during long
# runs — is the only thing that survives a full import.
#
# Captured at import time, before the autouse fixture stubs it out.
_REAL_COOKIE_REFRESH = YTMClient._maybe_refresh_browser_cookie


def _refreshing_client(cookie: str, monkeypatch, fresh: str | None):
    ytm = MagicMock()
    ytm._auth_headers = CaseInsensitiveDict({"cookie": cookie})
    client = _client_with_ytm(ytm)
    client._headers = {"cookie": cookie}
    monkeypatch.setattr(
        "migrator.ytm_client._browser_cookie_or_none", lambda *a, **k: fresh
    )
    return client


def test_browser_cookie_refresh_updates_live_session(
    isolated_settings, monkeypatch
) -> None:
    """The point of the whole mechanism: a run picks up rotated values without
    restarting, so it outlives the snapshot it started from."""
    client = _refreshing_client("SID=old", monkeypatch, "SID=new")

    _REAL_COOKIE_REFRESH(client, force=True)

    assert client._headers["cookie"] == "SID=new"
    # ytmusicapi reads its own dict when building the next request.
    assert client._ytm._auth_headers["cookie"] == "SID=new"


def test_browser_cookie_refresh_is_interval_limited(
    isolated_settings, monkeypatch
) -> None:
    """Decrypting the cookie store costs a keyring round-trip; doing it on
    every request would tax a multi-thousand-track import for nothing."""
    client = _refreshing_client("SID=old", monkeypatch, "SID=new")
    client._last_cookie_read = 10.0
    monkeypatch.setattr("migrator.ytm_client.time.monotonic", lambda: 11.0)

    _REAL_COOKIE_REFRESH(client)  # 1s since last read, interval is 300s

    assert client._headers["cookie"] == "SID=old"


def test_browser_cookie_refresh_survives_unreadable_store(
    isolated_settings, monkeypatch
) -> None:
    """A locked keyring mid-run must not kill an import — keep the cookie we
    have and try again on the next interval."""
    client = _client_with_ytm(MagicMock())
    client._headers = {"cookie": "SID=old"}
    monkeypatch.setattr(
        "migrator.ytm_client._browser_cookie_or_none",
        MagicMock(side_effect=RuntimeError("keyring locked")),
    )

    _REAL_COOKIE_REFRESH(client, force=True)  # must not raise

    assert client._headers["cookie"] == "SID=old"


def test_browser_cookie_refresh_disabled_in_file_mode(
    isolated_settings, monkeypatch
) -> None:
    """`cookie_source = "file"` must stay the pure-snapshot behaviour."""
    isolated_settings.toml.ytm.cookie_source = "file"
    client = _refreshing_client("SID=old", monkeypatch, "SID=new")

    _REAL_COOKIE_REFRESH(client, force=True)

    assert client._headers["cookie"] == "SID=old"


def test_build_headers_swaps_only_the_cookie(isolated_settings, monkeypatch) -> None:
    """x-goog-authuser / x-goog-pageid select the account when several are
    signed in, so they must survive the swap — only the cookie goes stale."""
    from migrator.ytm_client import build_headers

    path = isolated_settings.ytm_headers_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "cookie": "SID=stale", "x-goog-authuser": "2",
        "x-goog-pageid": "12345", "authorization": "SAPISIDHASH 1_a",
    }))
    monkeypatch.setattr(
        "migrator.ytm_client._browser_cookie_or_none", lambda *a, **k: "SID=live"
    )

    headers = build_headers()

    assert headers["cookie"] == "SID=live"
    assert headers["x-goog-authuser"] == "2"
    assert headers["x-goog-pageid"] == "12345"


# ---------- headers-file validation ----------
#
# ytmusicapi picks its auth type purely from the `authorization` header, and
# two of the three outcomes silently degrade instead of failing: a Bearer token
# is sent verbatim until it expires, and no authorization at all makes
# ytmusicapi rebuild base_headers from scratch — dropping the cookie and going
# anonymous. Both surface later as "this session is signed out", blaming a
# cookie that is perfectly valid. Catch them at load time instead.


def _write_headers(tmp_path, **extra):
    from migrator.ytm_client import load_headers

    p = tmp_path / "headers_auth.json"
    p.write_text(json.dumps({"cookie": "SAPISID=abc", "x-goog-authuser": "0", **extra}))
    return lambda: load_headers(p)


def test_load_headers_accepts_sapisidhash(tmp_path) -> None:
    """Even a long-stale timestamp is fine: only the word decides the auth
    type, and the hash is re-signed from the cookie on every request."""
    load = _write_headers(tmp_path, authorization="SAPISIDHASH 1_deadbeef")

    assert load()["authorization"].startswith("SAPISIDHASH")


def test_load_headers_rejects_bearer_token(tmp_path) -> None:
    load = _write_headers(tmp_path, authorization="Bearer ya29.some-token")

    with pytest.raises(ValueError, match="expires and is never refreshed"):
        load()


def test_load_headers_rejects_missing_authorization(tmp_path) -> None:
    load = _write_headers(tmp_path)

    with pytest.raises(ValueError, match="anonymously"):
        load()


# ---------- rotated session cookies ----------
#
# Google rotates the session-continuation cookies (__Secure-*SIDTS, the SIDCC
# family) every few minutes and returns the new values via Set-Cookie. A
# browser follows that rotation, which is why the tab stays signed in; but
# ytmusicapi pins the cookie into a frozen headers["cookie"] string and never
# reads the jar back. Once the pinned token ages out YouTube reports
# logged_in=0 while the browser is still signed in — the captured headers went
# *stale*, the account session never expired.


def _client_with_jar(cookie_header: str, jar_cookies: list[tuple[str, str, str]]):
    """Client whose ytmusicapi session jar holds `(name, value, domain)`."""
    import requests

    jar = requests.cookies.RequestsCookieJar()
    for name, value, domain in jar_cookies:
        jar.set(name, value, domain=domain, path="/")
    ytm = MagicMock()
    ytm._session.cookies = jar
    ytm._auth_headers = CaseInsensitiveDict({"cookie": cookie_header})
    client = _client_with_ytm(ytm)
    client._headers = {"cookie": cookie_header}
    return client


def test_adopts_rotated_cookie_values(isolated_settings) -> None:
    """The whole point: a rotated value replaces the captured one in place."""
    client = _client_with_jar(
        "SAPISID=stable; __Secure-3PSIDTS=OLD_TS; SIDCC=OLD_CC",
        [("__Secure-3PSIDTS", "NEW_TS", ".youtube.com"),
         ("SIDCC", "NEW_CC", ".google.com")],
    )

    client._sync_rotated_cookies()

    assert client._headers["cookie"] == (
        "SAPISID=stable; __Secure-3PSIDTS=NEW_TS; SIDCC=NEW_CC"
    )
    # ytmusicapi reads its own dict on the next request, so it must see it too.
    assert client._ytm._auth_headers["cookie"] == client._headers["cookie"]


def test_rotation_preserves_order_and_untouched_cookies(isolated_settings) -> None:
    """Only rotated names change. Cookie order is preserved so a diff of the
    headers file stays readable, and nothing we didn't authenticate with
    gets spliced in."""
    client = _client_with_jar(
        "HSID=a; SAPISID=b; __Secure-3PSIDTS=OLD",
        [("__Secure-3PSIDTS", "NEW", ".youtube.com"),
         ("UNRELATED", "nope", ".youtube.com")],
    )

    client._sync_rotated_cookies()

    assert client._headers["cookie"] == "HSID=a; SAPISID=b; __Secure-3PSIDTS=NEW"
    assert "UNRELATED" not in client._headers["cookie"]


def test_rotation_never_overwrites_identity_cookies(isolated_settings) -> None:
    """When the anti-abuse limiter degrades us to an anonymous session it
    answers with a signed-out cookie set. Adopting those would overwrite the
    user's real credentials on disk, turning a transient throttle into a
    lockout that only a fresh browser export could undo."""
    client = _client_with_jar(
        "SAPISID=real; SID=real; __Secure-3PSIDTS=OLD",
        [("SAPISID", "anon", ".youtube.com"),
         ("SID", "anon", ".youtube.com"),
         ("__Secure-3PSIDTS", "NEW", ".youtube.com")],
    )

    client._sync_rotated_cookies()

    # Continuation token adopted, identity left alone.
    assert client._headers["cookie"] == "SAPISID=real; SID=real; __Secure-3PSIDTS=NEW"


def test_rotation_ignores_foreign_domains(isolated_settings) -> None:
    """A jar entry from some other host must never overwrite our auth cookie."""
    client = _client_with_jar(
        "SAPISID=real",
        [("SAPISID", "attacker", ".evil.example.com")],
    )

    client._sync_rotated_cookies()

    assert client._headers["cookie"] == "SAPISID=real"


def test_rotation_persists_to_headers_file(isolated_settings) -> None:
    """The in-memory update keeps *this* run alive; the file write is what
    lets the next run start from a session that is still current."""
    path = isolated_settings.ytm_headers_path
    path.parent.mkdir(parents=True, exist_ok=True)
    isolated_settings.toml.ytm.cookie_source = "file"  # snapshot mode
    client = _client_with_jar(
        "SAPISID=stable; __Secure-3PSIDTS=OLD",
        [("__Secure-3PSIDTS", "NEW", ".youtube.com")],
    )
    client._headers["x-goog-authuser"] = "0"

    client._sync_rotated_cookies()

    written = json.loads(path.read_text())
    assert "__Secure-3PSIDTS=NEW" in written["cookie"]
    assert written["x-goog-authuser"] == "0"  # other headers survive the write


def test_rotation_write_is_rate_limited(isolated_settings) -> None:
    """SIDCC rotates on a large fraction of responses; one disk write per
    request would be pointless churn on a multi-thousand-track import."""
    path = isolated_settings.ytm_headers_path
    path.parent.mkdir(parents=True, exist_ok=True)
    isolated_settings.toml.ytm.cookie_source = "file"  # snapshot mode
    client = _client_with_jar(
        "__Secure-3PSIDTS=OLD",
        [("__Secure-3PSIDTS", "NEW", ".youtube.com")],
    )

    client._sync_rotated_cookies()
    first = path.read_text()
    # Rotate again immediately — in-memory must update, disk must not.
    client._ytm._session.cookies.set(
        "__Secure-3PSIDTS", "NEWER", domain=".youtube.com", path="/"
    )
    client._sync_rotated_cookies()

    assert "__Secure-3PSIDTS=NEWER" in client._headers["cookie"]
    assert path.read_text() == first  # still the previous write


def test_rotation_failure_never_breaks_a_call(isolated_settings) -> None:
    """The sync runs on every request including the error path — a surprise in
    the jar must not replace the error the caller was actually reporting."""
    ytm = MagicMock()
    ytm._session.cookies = object()  # not iterable
    client = _client_with_ytm(ytm)
    client._headers = {"cookie": "SAPISID=x"}

    client._sync_rotated_cookies()  # must not raise

    assert client._headers["cookie"] == "SAPISID=x"


def test_retry_loop_syncs_cookies_on_success(isolated_settings) -> None:
    """Wiring check: the happy path must sync too. An import that only
    refreshed on errors would still go stale during a clean run."""
    client = _client_with_jar(
        "__Secure-3PSIDTS=OLD",
        [("__Secure-3PSIDTS", "NEW", ".youtube.com")],
    )

    result = client._call_with_retry(
        lambda: "ok", backoff=1, max_retries=3, retry_on=("rate",), label="probe"
    )

    assert result == "ok"
    assert client._headers["cookie"] == "__Secure-3PSIDTS=NEW"


# ---------- signed-out detection ----------


def test_refresh_session_unpins_hardcoded_visitor_id(isolated_settings) -> None:
    """Dropping the cache alone is inert: base_headers only re-fetches the
    visitor id when the key is ABSENT, and headers captured from DevTools pin
    it. The key must be removed from both header dicts or the refresh is a
    no-op that only writes a log line."""
    class _FakeYTM:
        def __init__(self) -> None:
            self._auth_headers = {"x-goog-visitor-id": "stale_v1", "cookie": "c"}

        @property
        def base_headers(self):
            return self._auth_headers

        @base_headers.deleter
        def base_headers(self) -> None:
            pass

    fake = _FakeYTM()
    client = _client_with_ytm(fake)
    client._headers = {"x-goog-visitor-id": "stale_v1", "cookie": "c"}

    client._refresh_session()

    assert "x-goog-visitor-id" not in fake._auth_headers
    assert "x-goog-visitor-id" not in client._headers
    assert "cookie" in fake._auth_headers  # nothing else disturbed


def _browse_response(logged_in: str) -> dict:
    return {
        "responseContext": {
            "serviceTrackingParams": [
                {
                    "service": "GFEEDBACK",
                    "params": [
                        {"key": "browse_id", "value": "FEmusic_liked_playlists"},
                        {"key": "logged_in", "value": logged_in},
                    ],
                }
            ]
        }
    }


def test_assert_signed_in_raises_when_logged_out(isolated_settings) -> None:
    """logged_in=0 means reads succeed anonymously but every write is rejected
    as an ownership error. Fail before the first write, naming the real cause."""
    from migrator.ytm_client import YTMSignedOutError

    ytm = MagicMock()
    ytm._send_request.return_value = _browse_response("0")
    client = _client_with_ytm(ytm)

    with pytest.raises(YTMSignedOutError, match="signed out"):
        client.assert_signed_in()


def test_assert_signed_in_passes_when_logged_in(isolated_settings) -> None:
    ytm = MagicMock()
    ytm._send_request.return_value = _browse_response("1")
    client = _client_with_ytm(ytm)

    client.assert_signed_in()  # must not raise


def test_assert_signed_in_tolerates_probe_failure(isolated_settings) -> None:
    """A throttled or reshaped probe response is inconclusive, not a failure —
    it must never block an otherwise valid run."""
    ytm = MagicMock()
    ytm._send_request.side_effect = _non_json_error()
    client = _client_with_ytm(ytm)

    client.assert_signed_in()  # must not raise


def test_assert_signed_in_skipped_in_dry_run(isolated_settings) -> None:
    """Dry runs make no writes, so they must not spend a network call."""
    ytm = MagicMock()
    client = _client_with_ytm(ytm)
    client.dry_run = True

    client.assert_signed_in()

    ytm._send_request.assert_not_called()


def test_non_throttle_error_is_not_retried(isolated_settings, monkeypatch) -> None:
    """A non-throttle exception raises immediately without any backoff sleep."""
    sleeps: list[float] = []
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: sleeps.append(s))

    ytm = MagicMock()
    ytm.search.side_effect = ValueError("totally unrelated bug")
    client = _client_with_ytm(ytm)

    # search_songs swallows non-debug errors → [], but must NOT have retried.
    res = client.search_songs("q")
    assert res == []
    assert ytm.search.call_count == 1
    assert sleeps == []

# --- Duplicate rejection -----------------------------------------------------
#
# YouTube's playlist edit is atomic on duplicates: if any videoId in a batch is
# repeated within the batch, or is already in the playlist, it answers
# STATUS_FAILED and adds *nothing* — including the tracks that were fine.
# ytmusicapi signals this by returning the raw response dict instead of its
# normal {"status", "playlistEditResults"} shape. Verified against the live API:
# sending [C, C, D] to an empty playlist added neither C nor D.
#
# This used to be reported as "accepted fewer items than sent (dedupe …)",
# which read like YTM had safely skipped known tracks — so whole playlists
# silently imported 0 tracks while the run reported success.

_REJECTED_RESPONSE = {
    "responseContext": {},
    "status": "STATUS_FAILED",
    "actions": [],
    "trackingParams": "abc",
}


def test_add_playlist_items_isolates_the_offending_track(
    isolated_settings, monkeypatch
) -> None:
    """One poisoned id must cost one track, not the whole batch.

    YouTube canonicalises videoIds on add, so a collision can be invisible to us
    until it refuses. Bisecting asks it which id it actually means, so the other
    49 in a batch of 50 still land.
    """
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: None)
    poison = "v3"

    def fake_add(_pid, chunk):
        if poison in chunk:
            return _REJECTED_RESPONSE
        return _add_success(chunk)

    ytm = MagicMock()
    ytm.add_playlist_items.side_effect = fake_add
    client = _client_with_ytm(ytm)

    added = client.add_playlist_items("PL_X", ["v1", "v2", "v3", "v4"])

    assert sorted(added) == ["v1", "v2", "v4"]  # only the offender is lost
    assert client.last_add_rejected == 1
    assert client.rejected_video_ids == [poison]
    assert client.last_add_aborted is False  # not throttling — don't tell the
    #                                          user to wait and re-run


def test_rejected_track_is_not_recorded_as_added(isolated_settings, monkeypatch) -> None:
    """A refusal can also mean "video unavailable", so never record it.

    Recording a phantom subtracts it from every future run and permanently skips
    a track that never landed — strictly worse than re-isolating it next run.
    """
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: None)
    ytm = MagicMock()
    ytm.add_playlist_items.return_value = _REJECTED_RESPONSE
    client = _client_with_ytm(ytm)

    added = client.add_playlist_items("PL_X", ["v1", "v2"])

    assert added == []
    assert client.last_add_rejected == 2  # both isolated, neither recorded
    assert sorted(client.rejected_video_ids) == ["v1", "v2"]


def test_bisection_terminates_on_single_id(isolated_settings) -> None:
    """A one-id batch must be reported, not split forever."""
    ytm = MagicMock()
    ytm.add_playlist_items.return_value = _REJECTED_RESPONSE
    client = _client_with_ytm(ytm)

    added = client.add_playlist_items("PL_X", ["only"])

    assert added == []
    assert client.rejected_video_ids == ["only"]
    assert ytm.add_playlist_items.call_count == 1


def test_add_playlist_items_rejection_does_not_stop_later_batches(
    isolated_settings, monkeypatch
) -> None:
    """A poisoned batch must not cost the batches behind it.

    A refusal is a statement about that batch's contents, not about the account,
    so everything queued after it is still worth sending.
    """
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: None)
    isolated_settings.toml.importing.batch_size = 2
    poisoned = {"v1", "v2"}

    def fake_add(_pid, chunk):
        if poisoned.intersection(chunk):
            return _REJECTED_RESPONSE
        return _add_success(chunk)

    ytm = MagicMock()
    ytm.add_playlist_items.side_effect = fake_add
    client = _client_with_ytm(ytm)

    added = client.add_playlist_items("PL_X", ["v1", "v2", "v3", "v4"])

    assert added == ["v3", "v4"]  # the clean batch still lands
    assert client.last_add_rejected == 2
    assert sorted(client.rejected_video_ids) == ["v1", "v2"]


def test_add_playlist_items_clears_rejected_count_per_call(isolated_settings) -> None:
    """Stale counts would make a clean run report the previous run's failures."""
    ytm = MagicMock()
    ytm.add_playlist_items.side_effect = [
        _REJECTED_RESPONSE,
        _add_success(["v9"]),
    ]
    client = _client_with_ytm(ytm)

    client.add_playlist_items("PL_X", ["v1"])
    assert client.last_add_rejected == 1
    assert client.rejected_video_ids == ["v1"]

    client.add_playlist_items("PL_X", ["v9"])
    assert client.last_add_rejected == 0
    assert client.rejected_video_ids == []


# --- Live playlist reconciliation -------------------------------------------


def test_playlist_video_ids_reads_live_contents(isolated_settings) -> None:
    ytm = MagicMock()
    ytm.get_playlist.return_value = {
        "tracks": [{"videoId": "a"}, {"videoId": "b"}, {"videoId": None}, {}]
    }
    client = _client_with_ytm(ytm)

    assert client.playlist_video_ids("PL_X") == {"a", "b"}


def test_playlist_video_ids_falls_back_to_empty_on_error(
    isolated_settings, monkeypatch
) -> None:
    """An unreadable playlist must degrade to the recorded set, not look empty.

    Returning {} here means the caller keeps using `imported_tracks` — the old
    behaviour. Raising, or claiming the playlist is empty, would be worse: the
    latter re-sends every track and guarantees the duplicate rejection above.
    """
    monkeypatch.setattr("migrator.ytm_client.time.sleep", lambda s: None)
    ytm = MagicMock()
    ytm.get_playlist.side_effect = _rate_limit_error()
    client = _client_with_ytm(ytm)

    assert client.playlist_video_ids("PL_X") == set()


def test_playlist_video_ids_dry_run_skips_api(isolated_settings) -> None:
    ytm = MagicMock()
    client = _client_with_ytm(ytm)
    client.dry_run = True

    assert client.playlist_video_ids("PL_X") == set()
    ytm.get_playlist.assert_not_called()
