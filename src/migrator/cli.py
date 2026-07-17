"""CLI entry point.

Typer app with subcommands. Top-level invocation prints a status dashboard.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import get_settings
from .db import (
    Playlists,
    Tracks,
    auth_ok,
    get_session,
    list_playlists,
    set_ytm_playlist_id,
    upsert_auth,
)
from .logging_setup import get_logger, setup_logging
from .matching import MatchRequest, Matcher, persist_match
from .spotify_client import (
    ensure_client as ensure_spotify_client,
    iter_playlist_tracks,
    list_playlists as spotify_list_playlists,
    reset_auth as spotify_reset,
    run_auth_flow as spotify_auth_flow,
)
from .ytm_client import (
    YTMClient,
    ensure_client as ensure_ytm_client,
    reset_auth as ytm_reset,
    run_auth_flow as ytm_auth_flow,
)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    help="Spotify → YouTube Music playlist migrator.",
    pretty_exceptions_enable=False,
)

auth_app = typer.Typer(help="Authenticate with Spotify or YouTube Music.")
app.add_typer(auth_app, name="auth")

console = Console()
log = get_logger("cli")


def _print_status() -> None:
    settings = get_settings()
    sp_ok = auth_ok("spotify")
    ytm_ok = auth_ok("ytm")

    table = Table(title=f"spotify-to-ytmusic v{__version__}", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Data dir", str(settings.data_dir))
    table.add_row("DB", str(settings.db_path))
    table.add_row("Web UI", f"http://{settings.host}:{settings.port}")
    table.add_row("Spotify auth", "OK" if sp_ok else "[red]missing[/red]")
    table.add_row("YTM auth", "OK" if ytm_ok else "[red]missing[/red]")
    console.print(table)

    pls = list_playlists()
    if pls:
        pt = Table(title=f"Playlists ({len(pls)})")
        pt.add_column("Spotify ID")
        pt.add_column("Name")
        pt.add_column("Tracks")
        pt.add_column("YTM ID")
        pt.add_column("Imported")
        for p in pls:
            pt.add_row(
                p.playlist_id,
                p.name,
                str(p.track_count),
                p.ytm_playlist_id or "—",
                "yes" if p.imported_at else "no",
            )
        console.print(pt)
    else:
        console.print("\nNo playlists exported yet. Run: [bold]python -m migrator export[/bold]")


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"spotify-to-ytmusic {__version__}")
        raise typer.Exit()


def _print_batch_summary(
    verb_past: str, succeeded: list[str], failed: list[tuple[str, str]]
) -> None:
    """Print a one-line batch summary and exit non-zero if anything failed.

    `verb_past` is e.g. "matched", "imported", "migrated" — the past-tense
    form used in the success line. Failures are printed individually so the
    user can re-run the failed IDs by hand.
    """
    console.print(
        f"\n[bold]Batch summary:[/bold] {len(succeeded)} {verb_past}, "
        f"{len(failed)} failed"
    )
    for pid, reason in failed:
        console.print(f"  [red]FAIL[/red] {pid}: {reason}")
    if failed:
        raise typer.Exit(1)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Optional[bool] = typer.Option(
        None, "--version", callback=_version_callback, is_eager=True
    ),
) -> None:
    """Show status if no subcommand given."""
    setup_logging()
    if ctx.invoked_subcommand is None:
        _print_status()


@app.command()
def status() -> None:
    """Print a status dashboard (auth state, DB path, known playlists)."""
    _print_status()


# ---------- auth ----------

@auth_app.command("spotify")
def auth_spotify() -> None:
    """Walk through Spotify OAuth (browser redirect → local callback)."""
    setup_logging()
    spotify_auth_flow()


@auth_app.command("ytm")
def auth_ytm() -> None:
    """Walk through YouTube Music headers extraction."""
    setup_logging()
    ytm_auth_flow()


@auth_app.command("status")
def auth_status() -> None:
    """Show which services are authenticated."""
    sp = auth_ok("spotify")
    ytm = auth_ok("ytm")
    console.print(f"Spotify: {'OK' if sp else 'missing'}")
    console.print(f"YTM:     {'OK' if ytm else 'missing'}")
    if not sp:
        console.print("  → run: python -m migrator auth spotify")
    if not ytm:
        console.print("  → run: python -m migrator auth ytm")


@auth_app.command("reset")
def auth_reset(
    service: str = typer.Argument(..., help="'spotify' or 'ytm'"),
) -> None:
    """Forget stored credentials for a service."""
    if service == "spotify":
        spotify_reset()
    elif service in {"ytm", "ytmusic", "youtube-music"}:
        ytm_reset()
    else:
        raise typer.BadParameter("service must be 'spotify' or 'ytm'")
    console.print(f"[green]Reset {service} auth[/green]")


# ---------- list ----------

@app.command(name="list")
def list_cmd() -> None:
    """List Spotify playlists with track counts (does not export)."""
    setup_logging()
    if not auth_ok("spotify"):
        console.print("[red]Spotify not authenticated. Run: python -m migrator auth spotify[/red]")
        raise typer.Exit(1)
    sp = ensure_spotify_client()
    pls = spotify_list_playlists(sp)
    table = Table(title=f"Spotify playlists ({len(pls)})")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Owner")
    table.add_column("Tracks", justify="right")
    for p in pls:
        tracks = p.get("tracks") or {}
        total = tracks.get("total", "?")
        table.add_row(p["id"], p["name"], p.get("owner", {}).get("display_name", ""), str(total))
    console.print(table)


# ---------- export ----------

@app.command()
def export(
    playlist_id: Optional[str] = typer.Argument(None, help="One playlist ID, or omit for all"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print plan; don't write DB."),
) -> None:
    """Snapshot Spotify playlist(s) + tracks to SQLite."""
    setup_logging()
    if not auth_ok("spotify"):
        console.print("[red]Spotify not authenticated.[/red]")
        raise typer.Exit(1)
    sp = ensure_spotify_client()
    pls = spotify_list_playlists(sp)
    if playlist_id:
        pls = [p for p in pls if p["id"] == playlist_id]
        if not pls:
            console.print(f"[red]No Spotify playlist with id {playlist_id}[/red]")
            raise typer.Exit(1)

    from sqlmodel import Session, select

    from .db import Tracks as T, get_engine

    engine = get_engine()
    for meta in pls:
        sid, items = iter_playlist_tracks(sp, meta["id"])
        console.print(f"[bold]{meta['name']}[/bold]  ({len(items)} tracks) snapshot={sid}")
        if dry_run:
            continue
        with Session(engine) as s:
            existing = s.get(Playlists, meta["id"])
            row = existing or Playlists(playlist_id=meta["id"], snapshot_id=sid, name=meta["name"])
            row.snapshot_id = sid
            row.name = meta["name"]
            row.description = meta.get("description") or ""
            row.owner = meta.get("owner", {}).get("display_name", "")
            row.track_count = len(items)
            row.is_collaborative = bool(meta.get("collaborative"))
            row.is_public = bool(meta.get("public"))
            row.spotify_url = meta.get("external_urls", {}).get("spotify", "")
            s.add(row)
            # Replace tracks for this snapshot (idempotent).
            old = s.exec(
                select(T).where(T.playlist_id == meta["id"], T.snapshot_id == sid)
            ).all()
            for o in old:
                s.delete(o)
            for i, t in enumerate(items):
                ext = t.get("external_ids") or {}
                artists = [a.get("name", "") for a in (t.get("artists") or [])]
                album = (t.get("album") or {}).get("name", "")
                s.add(
                    T(
                        playlist_id=meta["id"],
                        snapshot_id=sid,
                        position=i,
                        spotify_track_id=t["id"],
                        name=t.get("name", ""),
                        artists=json.dumps(artists),
                        album=album,
                        duration_ms=t.get("duration_ms") or 0,
                        isrc=ext.get("isrc", "") or "",
                        spotify_url=(t.get("external_urls") or {}).get("spotify", ""),
                        raw_json=json.dumps(t, ensure_ascii=False, default=str),
                    )
                )
            s.commit()
    console.print("[green]Export complete[/green]")


# ---------- match ----------

@app.command()
def match(
    playlist_id: Optional[str] = typer.Argument(
        None, help="One playlist ID, or omit to match every exported playlist."
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Run matching for one — or every — previously-exported playlist."""
    setup_logging()
    if not auth_ok("ytm"):
        console.print("[red]YTM not authenticated.[/red]")
        raise typer.Exit(1)
    ytm = ensure_ytm_client(dry_run=dry_run)
    matcher = Matcher(ytm)
    if playlist_id is not None:
        _run_matching(matcher, playlist_id, dry_run=dry_run)
        return

    pls = list_playlists()
    if not pls:
        console.print("[yellow]No exported playlists. Run `export` first.[/yellow]")
        raise typer.Exit(1)
    console.print(f"Matching {len(pls)} playlists …")
    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []
    for p in pls:
        console.print(f"\n[bold]== {p.name}[/bold]  ({p.playlist_id})")
        try:
            _run_matching(matcher, p.playlist_id, dry_run=dry_run)
            succeeded.append(p.playlist_id)
        except typer.Exit as e:
            # Honor intentional exits (e.g. "no tracks in DB"); not a failure.
            if e.code in (None, 0):
                succeeded.append(p.playlist_id)
            else:
                failed.append((p.playlist_id, f"exit code {e.code}"))
        except Exception as e:  # noqa: BLE001
            log.exception("matching failed", extra={"playlist_id": p.playlist_id})
            failed.append((p.playlist_id, str(e)))
    _print_batch_summary("matched", succeeded, failed)


def _run_matching(matcher: Matcher, playlist_id: str, *, dry_run: bool) -> None:
    from sqlmodel import Session, select

    from .db import Tracks as T, get_engine

    engine = get_engine()
    with Session(engine) as s:
        pl = s.get(Playlists, playlist_id)
        if not pl:
            console.print(f"[red]Playlist {playlist_id} not exported yet. Run `export` first.[/red]")
            raise typer.Exit(1)
        tracks = s.exec(
            select(T).where(T.playlist_id == playlist_id, T.snapshot_id == pl.snapshot_id)
            .order_by(T.position)
        ).all()
    if not tracks:
        console.print("[red]No tracks in DB for this playlist.[/red]")
        raise typer.Exit(1)

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
    console.print(f"Matching {len(requests)} tracks (concurrency={matcher.concurrency}) …")
    results = asyncio.run(matcher.match_many(requests))

    counts = {"accepted": 0, "review": 0, "skipped": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
        if not dry_run:
            persist_match(r, playlist_id=playlist_id, snapshot_id=pl.snapshot_id)
    console.print(
        f"  accepted={counts.get('accepted', 0)}  "
        f"review={counts.get('review', 0)}  "
        f"skipped={counts.get('skipped', 0)}"
    )
    console.print("Next: open the review UI for any 'review' rows: python -m migrator serve")


# ---------- import ----------

@app.command(name="import")
def import_cmd(  # `import` is a Python keyword; registered under explicit name
    playlist_id: Optional[str] = typer.Argument(
        None, help="One playlist ID, or omit to import every exported playlist."
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Push approved matches to YTM — for one playlist or every exported one."""
    setup_logging()
    if not auth_ok("ytm"):
        console.print("[red]YTM not authenticated.[/red]")
        raise typer.Exit(1)

    # Single-playlist path: keep the existing semantics intact so web.py's
    # /playlists/{id}/import endpoint (which calls this with a non-None id)
    # is byte-identical to before.
    if playlist_id is not None:
        ytm = ensure_ytm_client(dry_run=dry_run)
        _import_one(ytm, playlist_id, dry_run=dry_run, yes_assumed=yes)
        return

    # Batch path.
    pls = list_playlists()
    if not pls:
        console.print("[yellow]No exported playlists. Run `export` first.[/yellow]")
        raise typer.Exit(1)

    plan = _plan_batch_imports(pls, dry_run=dry_run)
    if not plan["to_process"]:
        console.print("[yellow]Nothing to import across all playlists.[/yellow]")
        raise typer.Exit(0)

    if not yes and not dry_run and plan["n_new"] > 0:
        confirm = typer.confirm(
            f"Create {plan['n_new']} new YTM playlists "
            f"with {plan['total_tracks']} total tracks?",
            default=True,
        )
        if not confirm:
            raise typer.Abort()

    ytm = ensure_ytm_client(dry_run=dry_run)
    console.print(f"Importing {len(plan['to_process'])} playlists …")
    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []
    for pid in plan["to_process"]:
        pl = next(p for p in pls if p.playlist_id == pid)
        console.print(f"\n[bold]== {pl.name}[/bold]  ({pid})")
        try:
            _import_one(ytm, pid, dry_run=dry_run, yes_assumed=True)
            succeeded.append(pid)
        except typer.Exit as e:
            if e.code in (None, 0):
                succeeded.append(pid)
            else:
                failed.append((pid, f"exit code {e.code}"))
        except Exception as e:  # noqa: BLE001
            log.exception("import failed", extra={"playlist_id": pid})
            failed.append((pid, str(e)))
    _print_batch_summary("imported", succeeded, failed)


def _plan_batch_imports(
    pls: list[Playlists], *, dry_run: bool
) -> dict[str, Any]:
    """Decide which playlists actually have work to do, and tally new-playlist cost."""
    from sqlmodel import Session, select

    from .db import Matches, Tracks as T, get_engine

    engine = get_engine()
    to_process: list[str] = []
    n_new = 0
    total_tracks = 0
    for p in pls:
        with Session(engine) as s:
            pl = s.get(Playlists, p.playlist_id)
            if pl is None:
                continue
            rows = s.exec(
                select(Matches, T)
                .join(
                    T,
                    (T.playlist_id == Matches.playlist_id)
                    & (T.snapshot_id == Matches.snapshot_id)
                    & (T.spotify_track_id == Matches.spotify_track_id),
                )
                .where(Matches.playlist_id == p.playlist_id)
                .where(Matches.snapshot_id == pl.snapshot_id)
                .where(Matches.status.in_(["accepted", "manual_accepted"]))
            ).all()
        chosen = [m for m, _ in rows if m.chosen_video_id]
        if not chosen:
            continue
        to_process.append(p.playlist_id)
        if not pl.ytm_playlist_id:
            n_new += 1
            total_tracks += len(chosen)
    return {"to_process": to_process, "n_new": n_new, "total_tracks": total_tracks}


def _import_one(
    ytm: YTMClient, playlist_id: str, *, dry_run: bool, yes_assumed: bool
) -> None:
    """Per-playlist import. Raises typer.Exit on expected skip conditions.

    `yes_assumed=True` means we've already obtained user consent for any
    new-playlist creation upstream, so skip the per-call confirm prompt.
    """
    from sqlmodel import Session, select

    from .db import ImportedTracks, Matches, Tracks as T, get_engine

    engine = get_engine()
    with Session(engine) as s:
        pl = s.get(Playlists, playlist_id)
        if not pl:
            console.print(f"[red]Playlist {playlist_id} not exported.[/red]")
            raise typer.Exit(1)
        # If we've already imported this playlist, resume against the existing YTM ID.
        ytm_pid = pl.ytm_playlist_id
        already = set()
        if ytm_pid:
            already = {
                row.video_id
                for row in s.exec(
                    select(ImportedTracks).where(ImportedTracks.ytm_playlist_id == ytm_pid)
                ).all()
            }

        # Choose video_ids: accepted + manual_accepted.
        rows = s.exec(
            select(Matches, T)
            .join(T, (T.playlist_id == Matches.playlist_id) & (T.snapshot_id == Matches.snapshot_id)
                  & (T.spotify_track_id == Matches.spotify_track_id))
            .where(Matches.playlist_id == playlist_id)
            .where(Matches.snapshot_id == pl.snapshot_id)
            .where(Matches.status.in_(["accepted", "manual_accepted"]))
        ).all()
        chosen = [(m, t) for m, t in rows if m.chosen_video_id]

    if not chosen:
        console.print("[yellow]Nothing to import (no accepted matches). Run `match` + review.[/yellow]")
        raise typer.Exit(0)

    if ytm_pid:
        console.print(f"Resuming into existing YTM playlist [bold]{ytm_pid}[/bold] ({len(already)} already imported)")
    else:
        if not yes_assumed and not dry_run:
            confirm = typer.confirm(
                f"Create new YTM playlist '{pl.name}' with {len(chosen)} tracks?", default=True
            )
            if not confirm:
                raise typer.Abort()

    if not ytm_pid:
        ytm_pid = ytm.create_playlist(
            title=pl.name,
            description=pl.description or "Migrated from Spotify",
            privacy_status=get_settings().toml.importing.default_privacy,
        )
        if not dry_run:
            set_ytm_playlist_id(playlist_id, ytm_pid)

    todo = [m.chosen_video_id for m, _ in chosen if m.chosen_video_id and m.chosen_video_id not in already]
    if not todo:
        console.print("[green]Already fully imported.[/green]")
        raise typer.Exit(0)

    console.print(f"Adding {len(todo)} tracks to YTM playlist {ytm_pid} …")
    added = ytm.add_playlist_items(ytm_pid, todo)

    if not dry_run:
        with Session(engine) as s:
            for m, t in chosen:
                if m.chosen_video_id in added and m.chosen_video_id not in already:
                    s.add(
                        ImportedTracks(
                            playlist_id=playlist_id,
                            ytm_playlist_id=ytm_pid,
                            video_id=m.chosen_video_id,
                            spotify_track_id=t.spotify_track_id,
                        )
                    )
            pl_row = s.get(Playlists, playlist_id)
            if pl_row is not None:
                from datetime import datetime, timezone

                pl_row.imported_at = datetime.now(timezone.utc)
                s.add(pl_row)
            s.commit()
    console.print(f"[green]Imported {len(added)}/{len(todo)} tracks[/green]")


# `import` is a Python keyword so we use the explicit `name="import"` above.


# ---------- run ----------

@app.command()
def run(
    playlist_id: Optional[str] = typer.Argument(
        None, help="One playlist ID, or omit to run the full pipeline over every playlist."
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """End-to-end: export → match → (review) → import. Skips review queue.

    With no playlist_id, exports every Spotify playlist, then matches + imports
    each one in turn. Per-playlist failures are logged and the batch continues.
    """
    setup_logging()
    if not auth_ok("spotify"):
        console.print("[red]Spotify not authenticated.[/red]")
        raise typer.Exit(1)
    if not auth_ok("ytm"):
        console.print("[red]YTM not authenticated.[/red]")
        raise typer.Exit(1)

    # Step 1: export (already iterates all Spotify playlists when playlist_id is None).
    export(playlist_id=playlist_id, dry_run=dry_run)

    # Step 2: match + import. Single-playlist path keeps the original behavior
    # (review-queue abort, single-shot summary). Batch path continues past errors.
    pls = [playlist_id] if playlist_id is not None else [p.playlist_id for p in list_playlists()]
    if not pls:
        console.print("[yellow]No playlists to process.[/yellow]")
        raise typer.Exit(0)

    ytm = ensure_ytm_client(dry_run=dry_run)
    matcher = Matcher(ytm)
    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []
    for pid in pls:
        try:
            _run_pipeline_for_one(matcher, pid, dry_run=dry_run)
            succeeded.append(pid)
        except typer.Exit as e:
            if e.code in (None, 0):
                succeeded.append(pid)
            else:
                failed.append((pid, f"exit code {e.code}"))
        except Exception as e:  # noqa: BLE001
            log.exception("pipeline failed", extra={"playlist_id": pid})
            failed.append((pid, str(e)))
    if playlist_id is not None:
        return  # single-playlist mode preserves the original exit codes
    _print_batch_summary("migrated", succeeded, failed)


def _run_pipeline_for_one(matcher: Matcher, playlist_id: str, *, dry_run: bool) -> None:
    """Run matching, then import (skipping on review rows) for one playlist.

    Mirrors the original `run` body. On any review rows left, abort the
    auto-import step so the user can resolve them in the UI first — but
    treat that abort as a successful pipeline step (code 0).
    """
    _run_matching(matcher, playlist_id, dry_run=dry_run)
    from sqlmodel import Session, select

    from .db import Matches, get_engine

    engine = get_engine()
    with Session(engine) as s:
        review_count = len(
            s.exec(
                select(Matches).where(
                    Matches.playlist_id == playlist_id, Matches.status == "review"
                )
            ).all()
        )
    if review_count:
        console.print(
            f"[yellow]{review_count} tracks in review queue. "
            f"Open the UI to resolve them, then re-run `import {playlist_id}`.[/yellow]"
        )
        raise typer.Exit(0)
    import_cmd(playlist_id=playlist_id, dry_run=dry_run, yes=True)


# ---------- serve ----------

@app.command()
def serve(
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """Run the local FastAPI web UI."""
    setup_logging()
    import uvicorn

    from .config import get_settings
    from .web import create_app

    settings = get_settings()
    create_app()  # build once to log auth state, then serve
    uvicorn.run(
        "migrator.web:app",
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    app()