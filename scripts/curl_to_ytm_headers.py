#!/usr/bin/env python3
"""Convert a "Copy as cURL (bash)" string from Chrome DevTools into
.migrator/headers_auth.json.

Usage:
    # 1. Open https://music.youtube.com in Chrome, signed in.
    # 2. DevTools → Network → right-click a POST row → Copy → "Copy as cURL (bash)".
    # 3. Feed the clipboard to this script:
    ./scripts/curl_to_ytm_headers.py --clip                  # read from system clipboard
    ./scripts/curl_to_ytm_headers.py /tmp/ytm.curl          # read from a file
    pbpaste | ./scripts/curl_to_ytm_headers.py               # read from stdin
    ./scripts/curl_to_ytm_headers.py 'curl "..." -H ...'    # read from argv

The script writes a JSON object containing only the request headers
ytmusicapi actually uses (cookie, authorization, x-goog-*, x-origin, origin,
content-type, user-agent). Browser-noise headers (accept-*, sec-*, dnt,
priority, referer, sec-fetch-*, content-encoding) are discarded.

Default output path is .migrator/headers_auth.json next to the repo root;
override with --out PATH. Pass --print to also echo the JSON to stdout.
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

# Headers ytmusicapi actually needs (cookie + x-goog-authuser are required; the
# rest match the reference file .migrator/headers_auth.json was generated from).
KEEP_HEADERS: frozenset[str] = frozenset(
    {
        "cookie",
        "authorization",
        "x-goog-authuser",
        "x-goog-pageid",
        "x-origin",
        "origin",
        "content-type",
        "user-agent",
    }
)

# x-goog-visitor-id is deliberately absent from KEEP_HEADERS. ytmusicapi fetches
# a fresh one on demand, but only when the key is missing — pinning the captured
# value makes that refresh (and YTMClient._refresh_session) permanently inert, so
# a rotated visitor id yields 401s no amount of retrying can clear.


def _strip_curl_command(command: str) -> str:
    """Drop the leading ``curl`` (with optional backslash-continuations) so
    shlex.split sees only the args."""
    # Chrome's "Copy as cURL (bash)" wraps long lines with backslash-newlines,
    # preserves single-quoted strings, and starts with "curl ".
    return command.lstrip().removeprefix("curl").lstrip()


def parse_curl(command: str) -> dict[str, str]:
    """Return {header-name: value} extracted from ``-H '…'`` and ``-b '…'``.

    The chrome "Copy as cURL (bash)" format always uses single-quoted values,
    so shlex's POSIX mode handles them correctly. We re-parse with a forgiving
    approach — if shlex fails (e.g. user pasted a single-line Windows curl),
    we fall back to a regex pull.
    """
    # Two passes: header pairs first (so we can keep only the KEEP set), then
    # the cookie jar.
    headers: dict[str, str] = {}
    cookie: str | None = None

    args: list[str]
    try:
        args = shlex.split(_strip_curl_command(command), posix=True)
    except ValueError:
        # Fallback: single-quoted strings may not survive shlex. Use a regex
        # sweep for -H '…' and -b '…' pairs in the raw text.
        import re

        args = []  # Unused on the fallback path.

        def _iter_pairs(flag: str) -> list[str]:
            # Match ``flag '…'`` allowing any character inside the quotes.
            return re.findall(rf"{re.escape(flag)}\s*'((?:\\'|[^'])*)'", command)

        for value in _iter_pairs("-H"):
            if ":" not in value:
                continue
            name, _, hdr_value = value.partition(":")
            headers[name.strip().lower()] = hdr_value.strip()
        cookies = _iter_pairs("-b")
        if cookies:
            cookie = cookies[0]
    else:
        i = 0
        while i < len(args):
            flag = args[i]
            if flag in ("-H", "--header") and i + 1 < len(args):
                raw = args[i + 1]
                if ":" in raw:
                    name, _, value = raw.partition(":")
                    headers[name.strip().lower()] = value.strip()
                i += 2
                continue
            if flag in ("-b", "--cookie") and i + 1 < len(args):
                cookie = args[i + 1]
                i += 2
                continue
            i += 1

    if cookie is not None:
        # -b takes precedence over a 'cookie' header (Chrome sets both from
        # the same jar; they always agree, but -b is the canonical source).
        headers["cookie"] = cookie
    return headers


def filter_headers(headers: dict[str, str]) -> dict[str, str]:
    """Drop browser-noise headers; preserve only those ytmusicapi needs."""
    return {
        name: value
        for name, value in headers.items()
        if name in KEEP_HEADERS
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert a Chrome DevTools 'Copy as cURL' string into "
        ".migrator/headers_auth.json.",
    )
    parser.add_argument(
        "source",
        nargs="?",
        help="Path to a file containing the cURL command, or the cURL "
        "command itself if it starts with 'curl '. Omit when using "
        "--clip or stdin.",
    )
    parser.add_argument(
        "--clip",
        action="store_true",
        help="Read the cURL command from the system clipboard "
        "(wl-paste / xclip / pbpaste).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Where to write headers_auth.json. Default: "
        "<repo>/.migrator/headers_auth.json",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        help="Also print the resulting JSON to stdout (handy for piping).",
    )
    return parser.parse_args(argv)


def _read_clipboard() -> str:
    """Best-effort clipboard read across X11 / Wayland / macOS."""
    import shutil
    import subprocess

    candidates: list[list[str]] = []
    if sys.platform == "darwin":
        candidates.append(["pbpaste"])
    else:
        # Try Wayland first, then X11.
        candidates.append(["wl-paste", "-n"])
        candidates.append(["xclip", "-selection", "clipboard", "-o"])
        candidates.append(["xsel", "--clipboard", "--output"])

    last_err: subprocess.CalledProcessError | FileNotFoundError | None = None
    for cmd in candidates:
        if not shutil.which(cmd[0]):
            continue
        try:
            return subprocess.run(
                cmd, check=True, capture_output=True, text=True
            ).stdout
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            last_err = e
            continue

    tool_list = ", ".join(c[0] for c in candidates)
    raise RuntimeError(
        f"Could not read the system clipboard (tried {tool_list}). "
        "Install one of them, or pass the cURL as a file path / stdin."
    ) from last_err


def _read_input(args: argparse.Namespace) -> str:
    if args.clip:
        return _read_clipboard()
    if args.source is not None:
        src = Path(args.source)
        if src.exists():
            return src.read_text(encoding="utf-8")
        # Treat the argv value as the cURL literal itself.
        return " ".join([args.source] if not args.source.startswith("curl") else [args.source])
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit(
        "No input: pass a cURL file path / literal, use --clip, or pipe via stdin."
    )


def default_output_path() -> Path:
    # scripts/curl_to_ytm_headers.py → <repo>/.migrator/headers_auth.json
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root / ".migrator" / "headers_auth.json"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    raw = _read_input(args)
    parsed = filter_headers(parse_curl(raw))

    if "cookie" not in parsed or "x-goog-authuser" not in parsed:
        missing = sorted(
            {"cookie", "x-goog-authuser"} - set(parsed)
        )
        print(
            f"warning: missing required headers {missing}; "
            "YTM auth will fail until you re-extract.",
            file=sys.stderr,
        )

    out_path: Path = args.out or default_output_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(parsed, indent=2, ensure_ascii=False)
    out_path.write_text(payload + "\n", encoding="utf-8")

    if args.print:
        print(payload)

    print(f"wrote {len(parsed)} headers → {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
