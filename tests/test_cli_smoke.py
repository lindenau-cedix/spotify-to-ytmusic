"""Smoke tests for the CLI surface — just enough to catch import errors
and broken help text."""
from __future__ import annotations

from typer.testing import CliRunner

from migrator.cli import app


runner = CliRunner()


def test_help_works():
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0
    assert "Spotify" in res.stdout or "spotify" in res.stdout


def test_status_command_runs(isolated_settings):
    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0
    assert "Spotify" in res.stdout
    assert "YTM" in res.stdout


def test_auth_subcommand_help():
    res = runner.invoke(app, ["auth", "--help"])
    assert res.exit_code == 0
    for sub in ("spotify", "ytm", "status", "reset"):
        assert sub in res.stdout