"""Tests for secret storage + DB layer. Smoke tests, not exhaustive."""
from __future__ import annotations

from migrator import secrets
from migrator.db import AuthState, Tracks, get_engine, get_session, upsert_auth


def test_store_and_load_secret_roundtrip(isolated_settings):
    secrets.store_secret("test-account", "shh")
    assert secrets.load_secret("test-account") == "shh"
    secrets.delete_secret("test-account")
    assert secrets.load_secret("test-account") is None


def test_auth_state_persists(isolated_settings):
    upsert_auth("test-service", True, "ok")
    with get_session() as s:
        row = s.get(AuthState, "test-service")
        assert row is not None and row.ok is True
        s.delete(row)
        s.commit()