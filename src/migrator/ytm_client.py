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
from typing import Any, Callable

import requests
import ytmusicapi

from .config import get_settings
from .db import upsert_auth
from .logging_setup import get_logger

log = get_logger("ytm")

REQUIRED_HEADER_KEYS = {"cookie", "x-goog-authuser"}

# Substrings (matched against a lowercased exception message) that mean the
# call is worth retrying. `search_songs` / `create_playlist` additionally retry
# on the auth-throttle set, because YouTube's anti-abuse limiter surfaces rapid
# create/search as a spurious 401 "You must be signed in" rather than a 429.
_RATE_LIMIT_SIGNATURES = ("rate", "429")
# Server-side wobble that is never permanent. HTTP 409 belongs here because
# YouTube answers a playlist edit with "409: Conflict" when edits arrive faster
# than it wants to serialise them — it is a pacing signal, not a bad request,
# and the identical payload succeeds once the playlist settles. The 5xx codes
# are matched with the "http " prefix so a videoId or a track count containing
# "500" can't masquerade as a server error.
_TRANSIENT_SIGNATURES = (
    "http 409", "conflict",
    "http 500", "http 502", "http 503", "http 504",
    "connection aborted", "connection reset", "connection error",
    "remote end closed", "timed out", "read timeout",
)
_AUTH_THROTTLE_SIGNATURES = ("401", "unauthorized", "signed in")
_IMPORT_RETRY = _RATE_LIMIT_SIGNATURES + _TRANSIENT_SIGNATURES
_SEARCH_CREATE_RETRY = _IMPORT_RETRY + _AUTH_THROTTLE_SIGNATURES


class YTMSignedOutError(RuntimeError):
    """The cookie parses fine but YouTube treats the session as signed out.

    Google accepts these requests with HTTP 200 and serves anonymous results,
    so reads look healthy right up until the first write, which comes back as
    ``401 … You are not authorized to edit this playlist.`` — an ownership
    error, not an obvious auth error. Detected up front by
    :meth:`YTMClient.assert_signed_in` so the failure names its own cause.
    """


class YTMNonJSONResponseError(RuntimeError):
    """YTM answered with a non-JSON body (HTML error page or empty response).

    ytmusicapi parses the response body as JSON *before* checking the HTTP
    status code, so a throttled request answered with an HTML 429/5xx page
    surfaces as a bare JSONDecodeError instead of a status error. Raised on
    retry exhaustion so the CLI shows something actionable instead of
    "Expecting value: line 1 column 1 (char 0)".
    """


def _is_throttle(e: Exception, retry_on: tuple[str, ...]) -> bool:
    """True when the exception is worth retrying rather than surfacing.

    A JSONDecodeError from inside ytmusicapi means the response body was not
    JSON — in practice always the anti-abuse limiter serving an HTML page —
    so it counts as a throttle regardless of the message signatures.

    Transport-level failures are matched by *type* as well as by message.
    YouTube drops the TCP connection outright when it decides we are pushing
    too hard (``ConnectionResetError: [Errno 104]``, which surfaces here as
    ``requests.exceptions.ConnectionError``), and the wrapped message text
    varies by urllib3 version — the type check is the reliable half.
    """
    if isinstance(e, (json.JSONDecodeError, YTMNonJSONResponseError)):
        return True
    if isinstance(e, (requests.exceptions.ConnectionError,
                      requests.exceptions.Timeout,
                      requests.exceptions.ChunkedEncodingError)):
        return True
    msg = str(e).lower()
    return any(sig in msg for sig in retry_on)


# How often at most to write refreshed cookies back to headers_auth.json.
_HEADERS_PERSIST_INTERVAL_S = 60.0

# Cookies that carry the account identity itself, as opposed to the rotating
# session-continuation values. Never adopted from a response: when YouTube's
# anti-abuse limiter degrades us to an anonymous session it answers with a
# signed-out cookie set, and adopting *those* would overwrite the user's real
# credentials in headers_auth.json — turning a transient throttle into a
# permanent lockout that only a fresh browser export could undo.
_IDENTITY_COOKIES = frozenset({
    "SID", "HSID", "SSID", "APISID", "SAPISID", "LOGIN_INFO",
    "__Secure-1PSID", "__Secure-3PSID",
    "__Secure-1PAPISID", "__Secure-3PAPISID",
})


def _is_google_domain(domain: str | None) -> bool:
    """True for the domains that may legitimately refresh our auth cookie."""
    if not domain:
        return True  # host-only cookie from the request we just made
    host = domain.lstrip(".").lower()
    return host == "youtube.com" or host == "google.com" or host.endswith(
        (".youtube.com", ".google.com")
    )


def _is_conflict(e: Exception) -> bool:
    """True for YouTube's ``HTTP 409: Conflict`` on a playlist edit.

    Worth distinguishing from a plain rate-limit because the remedy differs:
    a 409 is YouTube refusing to serialise this particular edit, and a smaller
    batch usually goes through where the full one keeps bouncing.
    """
    return "409" in str(e) or "conflict" in str(e).lower()


def _batch_rejected(res: Any) -> bool:
    """True when YouTube refused the whole edit rather than partly applying it.

    ``add_playlist_items`` is atomic on duplicates: if any videoId in the batch
    is already in the playlist *or* repeated within the batch itself, YouTube
    answers ``STATUS_FAILED`` and adds **nothing** — including the tracks that
    were perfectly fine. Measured directly: sending ``[C, C, D]`` to an empty
    playlist adds neither C nor D.

    ytmusicapi surfaces this by returning the raw response dict instead of its
    ``{"status": …, "playlistEditResults": …}`` shape, so a missing/failed
    status is the signal. Distinguished from dedupe because the remedy is
    completely different — and because reporting it as dedupe told the user
    their tracks were already safely in YTM when they were not there at all.
    """
    return isinstance(res, dict) and "SUCCEEDED" not in str(res.get("status", ""))


def _confirmed_added(res: Any, chunk: list[str]) -> list[str]:
    """The subset of ``chunk`` YouTube actually confirmed it added.

    ``ytmusicapi.add_playlist_items`` returns ``{"status": "STATUS_SUCCEEDED",
    "playlistEditResults": [...]}`` on success and the *raw response dict* on
    failure — never a list. An earlier version guarded on ``isinstance(res,
    list)``, which is therefore always False, so every batch was recorded as
    imported no matter what YouTube did with it. Those phantom rows are then
    subtracted on the next run, permanently skipping tracks that never landed.

    Falls back to the whole chunk only when the response carries no per-video
    detail to contradict it (unknown shape, or a success with no result list).
    """
    if not isinstance(res, dict):
        return list(chunk)  # unrecognised shape — keep the optimistic contract
    if "SUCCEEDED" not in str(res.get("status", "")):
        return []  # failure status, or the raw response dict = nothing landed
    ids = {
        entry.get("videoId")
        for entry in (res.get("playlistEditResults") or [])
        if isinstance(entry, dict)
    }
    ids.discard(None)
    if not ids:
        return list(chunk)  # succeeded, but YTM returned no per-video mapping
    return [v for v in chunk if v in ids]


def _is_auth_failure(e: Exception) -> bool:
    """True when the exception looks like a stale session, not throttling.

    YTM surfaces session drift as a 401 "you must be signed in". On the
    anti-abuse limiter this also fires for rapid create/search, but the
    distinguishing factor is that a real session failure persists across
    retries with the same headers — so we never retry auth failures with the
    same cached state, only after a visitor-id refresh (see ``_refresh_session``).
    """
    if not isinstance(e, ytmusicapi.exceptions.YTMusicServerError):
        return False
    msg = str(e).lower()
    return "401" in msg or "unauthorized" in msg or "signed in" in msg


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
        # Required, and it must be the SAPISIDHASH form — see _assert_browser_auth.
        # A Bearer token here expires and is never refreshed; omitting it
        # entirely makes ytmusicapi drop the cookie and go anonymous.
        "authorization": "<PASTE THE SAPISIDHASH ... AUTHORIZATION HEADER>",
        "origin": "https://music.youtube.com",
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
    _assert_browser_auth(raw)
    return raw


def _assert_browser_auth(raw: dict[str, Any]) -> None:
    """Reject header sets ytmusicapi would not treat as browser auth.

    ``determine_auth_type`` keys entirely off the ``authorization`` header:
    "SAPISIDHASH …" means BROWSER, "Bearer …" means OAUTH_CUSTOM_FULL, and
    *absent* falls through to OAUTH_CUSTOM_CLIENT. Only BROWSER re-signs each
    request from the cookie; the other two send whatever was captured, forever.
    Worse, OAUTH_CUSTOM_CLIENT builds ``base_headers`` from
    ``initialize_headers()`` instead of ours, so the cookie is dropped and
    every request goes out anonymous — which surfaces much later as the
    thoroughly misleading "this session is signed out", pointing at a cookie
    that is in fact perfectly valid.

    The SAPISIDHASH timestamp being hours stale is fine and expected: only the
    *presence* of the word decides the auth type, and the hash itself is
    recomputed per request from the cookie's ``__Secure-3PAPISID``.
    """
    auth = str(raw.get("authorization", ""))
    if "SAPISIDHASH" in auth:
        return
    if auth.startswith("Bearer"):
        raise ValueError(
            "YTM headers carry a 'Bearer' authorization header, which pins "
            "ytmusicapi to a token that expires and is never refreshed. Copy a "
            "request whose authorization header starts with 'SAPISIDHASH' "
            "instead — those are re-signed from your cookie on every request."
        )
    raise ValueError(
        "YTM headers have no 'authorization' header, so ytmusicapi would send "
        "every request anonymously (and later report the session as signed "
        "out). Re-copy from a music.youtube.com request that has an "
        "authorization header — POSTs to /youtubei/v1/ always do."
    )


def import_headers_from(src: Path) -> Path:
    """Copy a headers file the user provided into the data dir."""
    settings = get_settings()
    settings.ensure_data_dir()
    if not src.exists():
        raise FileNotFoundError(f"Headers file not found: {src}")
    dest = settings.ytm_headers_path
    # If the user already saved the file at the destination path, there is
    # nothing to copy — copying a file onto itself raises SameFileError.
    if not (dest.exists() and src.resolve() == dest.resolve()):
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
        # Monotonic timestamp of the last successful create_playlist call, used
        # to pace successive creations across a batch run. None = never created.
        self._last_create_monotonic: float | None = None
        # Set by add_playlist_items; see its docstring.
        self.last_add_aborted = False
        self.last_add_rejected = 0
        self.rejected_video_ids: list[str] = []
        # Rate-limits the headers_auth.json write in _persist_headers.
        self._last_headers_write: float | None = None
        # Monotonic stamp of the last live browser-cookie read. Seeded here
        # because ensure_client has just read one via build_headers.
        self._last_cookie_read: float | None = time.monotonic()

    @property
    def ytm(self) -> ytmusicapi.YTMusic:
        if self._ytm is None:
            self._ytm = ytmusicapi.YTMusic(auth=self._headers)
        return self._ytm

    def _sync_rotated_cookies(self) -> None:
        """Guarded wrapper — keeping the session fresh must never break a run.

        Sits on every request path, including the error path, so an unexpected
        jar shape here would otherwise replace whatever the call itself was
        reporting with a confusing traceback from the cookie bookkeeping.
        """
        try:
            self._adopt_rotated_cookies()
        except Exception:  # noqa: BLE001
            log.debug("rotated-cookie sync failed", exc_info=True)

    def _adopt_rotated_cookies(self) -> None:
        """Adopt the cookie values Google rotated during this run.

        This is what makes a long import outlive its captured headers. Google
        rotates the session-continuation cookies (``__Secure-1PSIDTS`` /
        ``__Secure-3PSIDTS`` and the ``SIDCC`` family) every few minutes and
        hands the new values back via ``Set-Cookie``. A browser follows that
        rotation, which is why the tab stays signed in indefinitely — but
        ytmusicapi pins the whole cookie into a frozen ``headers["cookie"]``
        string and passes an unrelated ``cookies={"SOCS": "CAI"}`` per request,
        so the rotated values land in the session jar and are never sent back.
        Once the pinned continuation token ages out, YouTube reports
        ``logged_in=0`` even though the underlying account session is fine and
        the browser is still happily signed in.

        So the "expired cookie" was never expired — it was *stale*. We re-read
        the jar after every call and splice the fresh values into the header
        string. Only names already present are updated: the jar also collects
        cookies we never authenticated with, and adopting those wholesale
        would change what we present rather than keep it current.
        """
        ytm = self._ytm
        if ytm is None:
            return
        jar = getattr(getattr(ytm, "_session", None), "cookies", None)
        if jar is None:
            return
        # Sorted so youtube.com entries are applied after google.com ones: a
        # name scoped to both domains should resolve to the YTM-facing value.
        fresh = {
            c.name: c.value
            for c in sorted(jar, key=lambda c: "youtube.com" in (c.domain or ""))
            if c.value and _is_google_domain(c.domain)
        }
        if not fresh:
            return
        header = self._headers.get("cookie") or ""
        pairs: list[tuple[str, str]] = []
        changed: list[str] = []
        for part in header.split(";"):
            name, _, value = part.strip().partition("=")
            if not name:
                continue
            if name not in _IDENTITY_COOKIES and name in fresh and fresh[name] != value:
                changed.append(name)
                value = fresh[name]
            pairs.append((name, value))
        if not changed:
            return

        rebuilt = "; ".join(f"{n}={v}" for n, v in pairs)
        self._headers["cookie"] = rebuilt
        # ytmusicapi parsed its own CaseInsensitiveDict at construction time;
        # for BROWSER auth `base_headers` returns that same object, so updating
        # it in place is enough to reach the next request.
        auth_headers = getattr(ytm, "_auth_headers", None)
        if auth_headers is not None:
            auth_headers["cookie"] = rebuilt
        log.debug("adopted rotated YTM cookies", extra={"names": changed})
        self._persist_headers()

    def _persist_headers(self) -> None:
        """Write the refreshed cookie back to headers_auth.json.

        Rate-limited: YouTube rotates ``SIDCC`` on a large fraction of
        responses, and the in-memory update is what keeps *this* run alive —
        the file write only matters for the next one. Written via a temp file
        and os.replace so an interrupted run can't leave a half-written
        headers file, which would lock the user out until they re-export.
        """
        if get_settings().toml.ytm.cookie_source != "file":
            # The browser is the source of truth; rewriting the snapshot would
            # be churn, and a snapshot captured from a throttled response is
            # worse than the one the user exported deliberately.
            return
        now = time.monotonic()
        if self._last_headers_write is not None and \
                now - self._last_headers_write < _HEADERS_PERSIST_INTERVAL_S:
            return
        self._last_headers_write = now
        target = get_settings().ytm_headers_path
        try:
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._headers, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(target)
        except OSError as e:  # noqa: BLE001
            # Never fatal: a read-only data dir must not kill a running import.
            log.warning("could not persist refreshed YTM headers", extra={"err": str(e)})

    def _maybe_refresh_browser_cookie(self, *, force: bool = False) -> None:
        """Re-read the browser's cookie so a long run outlives any snapshot.

        This is the part that actually fixes "session signed out mid-import".
        A single read at startup would still leave us pinned to values that
        stop being accepted ~15-20 minutes later; the browser keeps following
        Google's rotation, so re-reading periodically keeps us current for as
        long as the user stays signed in.

        Never fatal: if the store is briefly unreadable (keyring locked, the
        file being rewritten), keep using the cookie we have and try again on
        the next interval.
        """
        cfg = get_settings().toml.ytm
        if cfg.cookie_source == "file":
            return
        now = time.monotonic()
        if (
            not force
            and self._last_cookie_read is not None
            and now - self._last_cookie_read < cfg.refresh_interval_seconds
        ):
            return
        self._last_cookie_read = now
        try:
            cookie = _browser_cookie_or_none(cfg.cookie_source, cfg.browser)
        except Exception:  # noqa: BLE001
            log.debug("browser cookie refresh failed", exc_info=True)
            return
        if not cookie or cookie == self._headers.get("cookie"):
            return
        self._headers["cookie"] = cookie
        ytm = self._ytm
        if ytm is not None:
            auth_headers = getattr(ytm, "_auth_headers", None)
            if auth_headers is not None:
                auth_headers["cookie"] = cookie
            # ytmusicapi derives sapisid once at construction and re-signs every
            # request from it, so a rotated __Secure-3PAPISID has to be pushed
            # through too or the fresh cookie gets signed with the old key.
            try:
                from ytmusicapi.helpers import sapisid_from_cookie

                ytm.sapisid = sapisid_from_cookie(cookie)
            except Exception:  # noqa: BLE001
                log.debug("could not re-derive sapisid", exc_info=True)
        log.info("refreshed YTM cookie from browser")

    def _refresh_session(self) -> None:
        """Drop the cached visitor id so the next request re-fetches it.

        ytmusicapi binds ``X-Goog-Visitor-Id`` to the cookie + IP fingerprint
        via a one-shot ``cached_property``. When Google's anti-abuse rotates
        the visitor id mid-run, every subsequent call with the cached value
        returns 401 "you must be signed in" — a problem no amount of backoff
        will fix. Clearing the cache forces ``base_headers`` to re-scrape
        ``ytcfg`` on the next request, which is the only state that can drift
        between calls without the cookie changing. Reloading the headers file
        is more aggressive but loses the user's pacing state; this is enough.
        """
        # Dropping the cache alone is inert: ``base_headers`` re-fetches the
        # visitor id only when ``X-Goog-Visitor-Id`` is absent, and a headers
        # file captured from DevTools pins that key. Remove it from both our
        # copy and the dict ytmusicapi parsed (a separate CaseInsensitiveDict)
        # so neither can re-seed the stale value.
        self._headers.pop("x-goog-visitor-id", None)
        if self._ytm is None:
            return  # nothing to refresh; next .ytm access will build fresh
        auth_headers = getattr(self._ytm, "_auth_headers", None)
        if auth_headers is not None:
            auth_headers.pop("x-goog-visitor-id", None)
        # cached_property stores its value under the function name; deleting
        # it forces the descriptor to recompute on next access.
        try:
            del self._ytm.base_headers  # type: ignore[attr-defined]
        except AttributeError:
            pass
        log.info("YTM session refreshed (visitor id re-fetched on next call)")

    def logged_in_flag(self) -> str | None:
        """YouTube's own view of whether this session is authenticated.

        Reads ``logged_in`` out of ``responseContext.serviceTrackingParams`` on
        a cheap browse call. Returns ``"1"`` / ``"0"``, or ``None`` when the
        field is absent (shape drift — treat as inconclusive, not as failure).
        """
        resp = self.ytm._send_request(  # noqa: SLF001 — no public equivalent
            "browse", {"browseId": "FEmusic_liked_playlists"}
        )
        for svc in resp.get("responseContext", {}).get("serviceTrackingParams", []):
            for param in svc.get("params", []):
                if param.get("key") == "logged_in":
                    return str(param.get("value"))
        return None

    def assert_signed_in(self) -> None:
        """Fail fast when the cookie is signed out server-side.

        Deliberately non-fatal on probe errors: a throttled or reshaped
        response must not block an otherwise valid run. Only an explicit
        ``logged_in=0`` raises.
        """
        if self.dry_run:
            return
        try:
            flag = self.logged_in_flag()
        except Exception as e:  # noqa: BLE001
            log.warning("could not verify YTM sign-in state", extra={"err": str(e)})
            return
        if flag == "0":
            raise YTMSignedOutError(
                "YouTube Music reports this session as signed out (logged_in=0). "
                "The cookie in headers_auth.json is no longer authenticated, so "
                "every playlist edit will be rejected as 'not authorized'. "
                "Re-export your headers from a browser tab that is signed in to "
                "music.youtube.com, then re-run — the import resumes where it "
                "left off."
            )
        log.info("YTM session verified", extra={"logged_in": flag})

    def _call_with_retry(
        self,
        fn: Callable[[], Any],
        *,
        backoff: float,
        max_retries: int,
        retry_on: tuple[str, ...],
        label: str,
        recover_on_auth: bool = False,
        max_backoff_seconds: float | None = None,
    ) -> Any:
        """Call ``fn`` with bounded exponential backoff on throttle errors.

        Retries only when ``_is_throttle(e, retry_on)`` is true. Sleep grows
        as ``backoff * 2**attempt``, capped at ``max_backoff_seconds`` if
        given — a single retry sleep will never exceed the cap regardless of
        attempt count. Re-raises the last exception once retries are
        exhausted; any non-matching exception is re-raised immediately.

        When ``recover_on_auth`` is true (search/create) and the failure looks
        like a stale session (``_is_auth_failure``), the cached visitor id is
        dropped once before falling back to the throttle backoff loop. This
        is what makes the "1-2 playlists then 401" failure mode recover
        mid-batch instead of needing a full reauth.
        """
        last_exc: Exception | None = None
        auth_recovered = False
        for attempt in range(max_retries):
            # Interval-guarded, so this is a cheap no-op on almost every call.
            self._maybe_refresh_browser_cookie()
            try:
                result = fn()
            except Exception as e:  # noqa: BLE001
                # A throttled or failed reply is still a reply, and can carry
                # rotated session cookies. Sync before deciding what to do.
                self._sync_rotated_cookies()
                # Auth recovery is checked *before* the throttle filter, so a
                # caller can opt into the one-shot visitor-id refresh without
                # also putting "401" in its retry_on set. add_playlist_items
                # needs exactly that: recover from the anti-abuse limiter's
                # spurious 401, but never grind through a backoff ladder
                # against a genuine "not authorized to edit this playlist".
                recoverable_auth = (
                    recover_on_auth and not auth_recovered and _is_auth_failure(e)
                )
                if not recoverable_auth and not _is_throttle(e, retry_on):
                    raise
                last_exc = e
                if recoverable_auth:
                    log.warning(
                        "YTM session looks stale; refreshing cookie + visitor id",
                        extra={"op": label, "err": str(e)},
                    )
                    # Pull a fresh cookie before the visitor id: if the session
                    # really has moved on, only the browser has the new values.
                    self._maybe_refresh_browser_cookie(force=True)
                    self._refresh_session()
                    auth_recovered = True
                    continue  # retry immediately, no sleep
                if attempt == max_retries - 1:
                    break
                sleep_s = backoff * (2 ** attempt)
                if max_backoff_seconds is not None:
                    sleep_s = min(sleep_s, max_backoff_seconds)
                log.warning(
                    "YTM throttled; backing off",
                    extra={
                        "op": label,
                        "attempt": attempt,
                        "backoff_s": sleep_s,
                        "err": str(e)},
                )
                time.sleep(sleep_s)
            else:
                self._sync_rotated_cookies()
                return result
        assert last_exc is not None  # only reachable after a retryable failure
        if isinstance(last_exc, json.JSONDecodeError):
            raise YTMNonJSONResponseError(
                f"YouTube Music returned a non-JSON response for {label} after "
                f"{max_retries} attempts — this is almost always rate limiting. "
                f"Wait a while and re-run (the run resumes where it left off); "
                f"if it persists, refresh headers_auth.json."
            ) from last_exc
        raise last_exc

    def search_songs(self, query: str, limit: int = 5, *, debug: bool = False) -> list[dict[str, Any]]:
        """Top N song results. Returns list of YTM dicts with videoId, title, …

        Retries on YouTube throttling with a light exponential backoff (see
        ``[matching] search_retry_*`` config — deliberately faster than the
        import backoff because search runs per-track). On exhaustion (or any
        non-throttle error) logs at warning level and returns ``[]`` — this
        preserves the historical silent-degrade contract the matching pipeline
        depends on. Pass ``debug=True`` to log the full traceback and re-raise.
        """
        s = get_settings().toml.matching
        try:
            res = self._call_with_retry(
                lambda: self.ytm.search(query, filter="songs", limit=limit),
                backoff=s.search_retry_backoff_seconds,
                max_retries=s.search_retry_max_retries,
                retry_on=_SEARCH_CREATE_RETRY,
                label="search",
                recover_on_auth=True,
                max_backoff_seconds=s.search_retry_max_backoff_seconds,
            )
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

    def search_songs_strict(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Like ``search_songs`` but raises on throttle exhaustion.

        The default ``search_songs`` silently degrades to ``[]`` so a single
        failing search can't kill the matching pipeline — that's the right
        behaviour for "no result for this track" but the wrong one for "we
        just got throttled and every subsequent search will silently fail
        too." Use this variant when the caller can act on the failure
        (typically: stop and tell the user to wait).
        """
        s = get_settings().toml.matching
        res = self._call_with_retry(
            lambda: self.ytm.search(query, filter="songs", limit=limit),
            backoff=s.search_retry_backoff_seconds,
            max_retries=s.search_retry_max_retries,
            retry_on=_SEARCH_CREATE_RETRY,
            label="search",
            recover_on_auth=True,
            max_backoff_seconds=s.search_retry_max_backoff_seconds,
        )
        return list(res or [])

    def create_playlist(self, title: str, description: str = "",
                        privacy_status: str = "PRIVATE") -> str:
        if self.dry_run:
            log.info("DRY RUN: would create playlist", extra={"title": title})
            return "DRY_RUN_PLAYLIST_ID"

        imp = get_settings().toml.importing
        # Pace successive creations: YouTube throttles rapid create_playlist
        # calls with a spurious 401. If we created one recently, wait out the
        # remainder of the minimum interval before trying again.
        if self._last_create_monotonic is not None:
            elapsed = time.monotonic() - self._last_create_monotonic
            wait = imp.create_min_interval_seconds - elapsed
            if wait > 0:
                time.sleep(wait)

        try:
            res = self._call_with_retry(
                lambda: self.ytm.create_playlist(
                    title=title, description=description, privacy_status=privacy_status
                ),
                backoff=imp.rate_limit_backoff_seconds,
                max_retries=imp.rate_limit_max_retries,
                retry_on=_SEARCH_CREATE_RETRY,
                label="create_playlist",
                recover_on_auth=True,
            )
        except Exception as e:  # noqa: BLE001
            log.error("create_playlist failed", extra={"title": title, "err": str(e)})
            raise
        finally:
            # Stamp even on failure so a failed create still spaces the next one.
            self._last_create_monotonic = time.monotonic()
        # ytmusicapi returns the playlist id as a plain string on success,
        # but the type is documented as `Union[str, dict]` — on error it
        # returns the full response dict. Handle both shapes.
        if isinstance(res, str):
            pid = res
        elif isinstance(res, dict):
            pid = res.get("id") or ""
        else:
            pid = ""
        if not pid:
            raise RuntimeError(f"ytm.create_playlist returned no id: {res!r}")
        return pid

    def add_playlist_items(self, playlist_id: str, video_ids: list[str]) -> list[str]:
        """Add items in batches of `batch_size`. Returns list of video_ids
        that were successfully added (YTM may dedupe).

        Sets ``last_add_aborted`` when throttling cut the run short before every
        batch was attempted, and ``last_add_rejected`` / ``rejected_video_ids``
        for the individual tracks YouTube refused (isolated by bisecting the
        batch — see the rejection branch below). The return value alone cannot
        express either — a short list looks identical to "YTM deduped most of
        them" — and the caller needs the difference to decide between "done",
        "re-run to resume", and "these specific tracks did not land". Callers
        should read all three immediately after this returns.
        """
        self.last_add_aborted = False
        self.last_add_rejected = 0
        self.rejected_video_ids = []
        if self.dry_run:
            log.info(
                "DRY RUN: would add items to playlist",
                extra={"playlist_id": playlist_id, "count": len(video_ids)},
            )
            return list(video_ids)

        imp = get_settings().toml.importing

        # Worklist rather than a plain slice loop: a batch YouTube refuses with
        # 409 gets split in half and pushed back on the front, so the retry
        # keeps this playlist's remaining items in order.
        pending: list[list[str]] = [
            video_ids[i : i + imp.batch_size]
            for i in range(0, len(video_ids), imp.batch_size)
        ]
        added: list[str] = []
        first = True
        while pending:
            chunk = pending.pop(0)
            # Space out successive edits. YouTube's limiter reacts to *rate* of
            # playlist mutations, and back-to-back batches are what pushed the
            # earlier runs into 409s and dropped connections partway through a
            # playlist. Paying a couple of seconds per batch is far cheaper
            # than a 30s backoff plus a resumed run.
            if not first:
                time.sleep(imp.batch_interval_seconds)
            first = False
            try:
                res = self._call_with_retry(
                    lambda c=chunk: self.ytm.add_playlist_items(playlist_id, c),
                    backoff=imp.rate_limit_backoff_seconds,
                    max_retries=imp.rate_limit_max_retries,
                    retry_on=_IMPORT_RETRY,
                    label="add_playlist_items",
                    max_backoff_seconds=imp.rate_limit_max_backoff_seconds,
                    # One-shot visitor-id refresh only. 401 is deliberately
                    # absent from _IMPORT_RETRY, so a second 401 surfaces
                    # immediately instead of burning the backoff ladder — but
                    # sustained throttling does provoke a spurious "not
                    # authorized to edit this playlist" that the refresh fixes,
                    # and losing a whole playlist to it is the worse trade.
                    recover_on_auth=True,
                )
            except Exception as e:  # noqa: BLE001
                msg = str(e).lower()
                if "region" in msg or "not available" in msg:
                    # Region-restricted: nothing landed. Recording these as
                    # imported would subtract them from every future run, so
                    # they'd never be retried after a catalog/region change.
                    log.warning("region restriction on add", extra={"err": str(e)})
                    continue
                if _is_conflict(e) and len(chunk) > imp.min_batch_size:
                    # A 409 that survived the backoff loop is usually about
                    # this batch, not about the account — half of it typically
                    # goes through. Split and cool off. The `min_batch_size`
                    # floor bounds the recursion: without it a genuinely stuck
                    # playlist would degrade to one request per track, each
                    # with a full retry budget.
                    mid = len(chunk) // 2
                    pending[:0] = [chunk[:mid], chunk[mid:]]
                    log.warning(
                        "YTM kept refusing the batch; splitting",
                        extra={"size": len(chunk), "err": str(e)},
                    )
                    time.sleep(imp.conflict_cooldown_seconds)
                    continue
                if _is_throttle(e, _IMPORT_RETRY):
                    # Retries exhausted. Stop the whole playlist rather than
                    # hammering the limiter with the remaining batches — but
                    # flag it so the caller doesn't report a clean success.
                    # Re-running resumes from imported_tracks.
                    log.error("giving up on batch after retries", extra={"chunk": chunk[:5]})
                    self.last_add_aborted = True
                    break
                # Genuine error (bad request, auth, …): surface it.
                raise
            if _batch_rejected(res):
                # Hard refusal, not dedupe: nothing in this chunk landed.
                if len(chunk) > 1:
                    # Bisect to isolate the offender rather than lose the batch.
                    # We cannot predict this collision by comparing ids: YouTube
                    # *canonicalises* a videoId on add, so two distinct ids we
                    # hold can resolve to the same track, and a playlist read
                    # reports only the canonical one. Measured: adding
                    # "dQw4w9WgXcQ" stores it as "lYBUbBu4W08". Neither the
                    # dedupe of `todo` nor reconciliation against the live
                    # playlist can see that coming — only YouTube can, and it
                    # tells us by refusing. So we ask it which track it means.
                    mid = len(chunk) // 2
                    pending[:0] = [chunk[:mid], chunk[mid:]]
                    log.warning(
                        "batch refused; bisecting to isolate the duplicate",
                        extra={"size": len(chunk), "playlist_id": playlist_id},
                    )
                    continue
                # Down to a single id: this is the one YouTube objects to.
                # Deliberately NOT recorded as imported. It is almost certainly
                # already in the playlist under its canonical id, but a refusal
                # can also mean the video is unavailable — and recording a
                # phantom would subtract it from every future run and skip a
                # track that never landed. Costing a re-isolation next run is
                # the cheaper mistake.
                self.last_add_rejected += 1
                self.rejected_video_ids.append(chunk[0])
                log.error(
                    "YTM refused this track (STATUS_FAILED) — already in the "
                    "playlist under a different videoId, or unavailable",
                    extra={
                        "video_id": chunk[0],
                        "playlist_id": playlist_id,
                        "status": str(res.get("status", "")) if isinstance(res, dict) else "",
                    },
                )
                continue
            confirmed = _confirmed_added(res, chunk)
            added.extend(confirmed)
            if len(confirmed) < len(chunk):
                log.warning(
                    "YTM confirmed fewer items than sent (deduped)",
                    extra={"sent": len(chunk), "got": len(confirmed)},
                )
        return added

    def playlist_video_ids(self, playlist_id: str) -> set[str]:
        """Video ids currently in a YTM playlist, as YouTube sees them.

        ``imported_tracks`` records only what a run *confirmed*, so a run killed
        mid-playlist — or one whose batches YouTube refused — leaves tracks live
        on YTM that we never recorded. Re-sending those is not just wasteful:
        every one of them is a duplicate that fails its whole batch (see
        :func:`_batch_rejected`), so a few unrecorded tracks can block hundreds
        of good ones. Reconciling against the live playlist is what breaks that
        cycle, and is the fix CLAUDE.md's "known wart" note anticipated.

        Returns an empty set when the playlist can't be read, so the caller
        falls back to the recorded set — degrading to the old behaviour rather
        than treating an unreadable playlist as empty and re-sending everything.
        """
        if self.dry_run:
            return set()
        imp = get_settings().toml.importing
        try:
            res = self._call_with_retry(
                lambda: self.ytm.get_playlist(playlist_id, limit=None),
                backoff=imp.rate_limit_backoff_seconds,
                max_retries=imp.rate_limit_max_retries,
                retry_on=_SEARCH_CREATE_RETRY,
                label="get_playlist",
                recover_on_auth=True,
                max_backoff_seconds=imp.rate_limit_max_backoff_seconds,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "could not read live playlist contents; falling back to imported_tracks",
                extra={"playlist_id": playlist_id, "err": str(e)},
            )
            return set()
        tracks = (res or {}).get("tracks") or [] if isinstance(res, dict) else []
        return {
            t["videoId"]
            for t in tracks
            if isinstance(t, dict) and t.get("videoId")
        }


def _browser_cookie_or_none(source: str, browser: str) -> str | None:
    """Live browser cookie, or None when the configured source forbids it."""
    if source == "file":
        return None
    from .browser_cookies import BrowserCookieError, read_cookie_header

    try:
        return read_cookie_header(browser)
    except BrowserCookieError:
        if source == "browser":
            raise  # explicitly opted in — don't silently use a stale snapshot
        log.warning(
            "could not read live browser cookie; falling back to headers_auth.json",
            exc_info=True,
        )
        return None


def build_headers(*, allow_browser: bool = True) -> dict[str, Any]:
    """Assemble the request headers, preferring the browser's live cookie.

    Everything except the cookie still comes from headers_auth.json: the
    origin, the user agent, and — importantly — ``x-goog-authuser`` /
    ``x-goog-pageid``, which select the right account when the user is signed
    into several. Only the cookie is swapped, because only the cookie goes
    stale.
    """
    ytm_cfg = get_settings().toml.ytm
    headers = load_headers()
    if not allow_browser:
        return headers
    cookie = _browser_cookie_or_none(ytm_cfg.cookie_source, ytm_cfg.browser)
    if cookie:
        headers["cookie"] = cookie
        log.info("using live browser cookie", extra={"browser": ytm_cfg.browser})
    return headers


def ensure_client(*, dry_run: bool = False) -> YTMClient:
    return YTMClient(headers=build_headers(), dry_run=dry_run)