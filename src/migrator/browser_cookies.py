"""Read the live YouTube Music session cookie straight from the browser.

A cookie exported by hand from DevTools is a *snapshot*, and it stops
authenticating roughly 15-20 minutes later even though the browser it came
from stays signed in. Google rotates the session-continuation cookies
(``__Secure-1PSIDTS`` / ``__Secure-3PSIDTS``, the ``SIDCC`` family) through
``accounts.google.com``; the browser follows that rotation, the snapshot
cannot, and the pinned values eventually stop being accepted.

Reading the jar off our own responses does not rescue this: the ``youtubei``
endpoints we call never send those cookies back, so there is nothing to adopt
(measured across a full import — the only rotations we ever received were
``VISITOR_PRIVACY_METADATA``, ``YSC`` and ``__Secure-ROLLOUT_TOKEN``).

The browser's own cookie store is the only copy that stays current, so we read
it directly and re-read it periodically during long runs. Verified against a
snapshot that had already gone dead: same moment, same account, the file's
cookie returned ``"LOGGED_IN":false`` and the browser's returned
``"LOGGED_IN":true``.
"""
from __future__ import annotations

from .logging_setup import get_logger

log = get_logger("ytm")

# Cookies must be scoped to this domain to reach music.youtube.com.
_YTM_DOMAIN = "youtube.com"

# Without these the session is not authenticated, and YouTube says so only
# indirectly — by serving a normal HTTP 200 with anonymous content.
_REQUIRED = ("SAPISID", "__Secure-3PAPISID")


class BrowserCookieError(RuntimeError):
    """The browser cookie store could not be read or held no YTM session."""


def read_cookie_header(browser: str = "chrome") -> str:
    """Return a `Cookie:` header value built from the browser's live jar.

    Raises :class:`BrowserCookieError` with an actionable message rather than
    letting browser_cookie3's own errors surface — they describe keyring and
    sqlite internals, which say nothing about what the user should do.
    """
    try:
        import browser_cookie3
    except ImportError as e:  # pragma: no cover - dependency is declared
        raise BrowserCookieError(
            "browser_cookie3 is not installed. Run `make install`, or set "
            "[ytm] cookie_source = \"file\" in config.toml to go back to "
            "headers_auth.json."
        ) from e

    loader = getattr(browser_cookie3, browser, None)
    if loader is None:
        raise BrowserCookieError(
            f"Unknown browser {browser!r} for [ytm] browser. browser_cookie3 "
            f"supports chrome, chromium, firefox, edge, brave, opera, safari."
        )

    try:
        jar = loader(domain_name=_YTM_DOMAIN)
    except Exception as e:  # noqa: BLE001 - browser_cookie3 raises many types
        raise BrowserCookieError(
            f"Could not read {browser} cookies: {e}. On Linux the cookie store "
            f"is encrypted with a key from your login keyring, so this needs an "
            f"unlocked keyring session. Set [ytm] cookie_source = \"file\" to "
            f"fall back to headers_auth.json."
        ) from e

    # Later entries win: a cookie scoped to both .google.com and .youtube.com
    # should resolve to the YouTube-facing value.
    pairs = {c.name: c.value for c in sorted(jar, key=lambda c: _YTM_DOMAIN in (c.domain or ""))}
    missing = [k for k in _REQUIRED if k not in pairs]
    if missing:
        raise BrowserCookieError(
            f"{browser} has no YouTube Music session (missing {', '.join(missing)}). "
            f"Sign in to music.youtube.com in {browser} and try again."
        )
    return "; ".join(f"{n}={v}" for n, v in pairs.items() if v)
