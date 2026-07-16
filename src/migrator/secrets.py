"""Secret-storage helpers.

Strategy:
- Prefer the system keyring (macOS Keychain / Linux SecretService / Windows
  Credential Vault) via the `keyring` library.
- If no real keyring backend is available (headless server, broken D-Bus,
  CI runner), fall back to a Fernet-encrypted file under the data dir. The
  Fernet key itself lives in a sibling file with 0600 perms.

The fallback is *not* as strong as a real keyring. We surface this in the
README and emit a log warning when we have to use it.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import keyring
from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings
from .logging_setup import get_logger

KEYRING_SERVICE = "spotify-to-ytmusic"

log = get_logger("secrets")


def _real_keyring_available() -> bool:
    try:
        keyring.set_password(KEYRING_SERVICE, "__probe__", "ok")
        keyring.delete_password(KEYRING_SERVICE, "__probe__")
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("system keyring unavailable, using file fallback", extra={"err": str(e)})
        return False


def _fallback_key_path() -> Path:
    return get_settings().data_dir / "fallback.key"


def _fallback_payload_path() -> Path:
    return get_settings().data_dir / "fallback.bin"


def _ensure_fallback_key() -> bytes:
    p = _fallback_key_path()
    if p.exists():
        return p.read_bytes()
    k = Fernet.generate_key()
    p.write_bytes(k)
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    return k


def _fernet() -> Fernet:
    return Fernet(_ensure_fallback_key())


def store_secret(account: str, value: str) -> None:
    """Store `value` against `account` in keyring or fallback file."""
    if _real_keyring_available():
        keyring.set_password(KEYRING_SERVICE, account, value)
        return
    payload = _read_fallback()
    payload[account] = value
    _write_fallback(payload)


def load_secret(account: str) -> str | None:
    """Load a secret by account. Returns None if missing."""
    if _real_keyring_available():
        try:
            return keyring.get_password(KEYRING_SERVICE, account)
        except Exception:
            return None
    return _read_fallback().get(account)


def delete_secret(account: str) -> None:
    if _real_keyring_available():
        try:
            keyring.delete_password(KEYRING_SERVICE, account)
        except keyring.errors.PasswordDeleteError:
            pass
        return
    payload = _read_fallback()
    payload.pop(account, None)
    _write_fallback(payload)


def _read_fallback() -> dict[str, str]:
    p = _fallback_payload_path()
    if not p.exists():
        return {}
    try:
        raw = _fernet().decrypt(p.read_bytes())
    except InvalidToken:
        log.warning("fallback secrets file unreadable (bad key or tampering); starting empty")
        return {}
    import json

    return json.loads(raw.decode("utf-8"))


def _write_fallback(payload: dict[str, str]) -> None:
    import json

    encrypted = _fernet().encrypt(json.dumps(payload).encode("utf-8"))
    p = _fallback_payload_path()
    p.write_bytes(encrypted)
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)