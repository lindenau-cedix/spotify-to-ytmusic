"""Configuration: .env (secrets) + config.toml (non-secrets).

Loaded once at import. All paths resolve to absolute paths under the project root
unless the user supplies an absolute path.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve the project root as the parent of src/.
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]


def _resolve(path: str | Path) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (PROJECT_ROOT / p).resolve()
    return p


class EnvSettings(BaseSettings):
    """Secrets come from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    spotify_client_id: str = Field(default="", alias="SPOTIFY_CLIENT_ID")
    spotify_client_secret: str = Field(default="", alias="SPOTIFY_CLIENT_SECRET")
    spotify_redirect_uri: str = Field(
        default="http://localhost:8000/callback/spotify",
        alias="SPOTIFY_REDIRECT_URI",
    )
    migrator_data_dir: str = Field(default=".migrator", alias="MIGRATOR_DATA_DIR")
    migrator_db_path: str = Field(default=".migrator/migrator.db", alias="MIGRATOR_DB_PATH")
    migrator_host: str = Field(default="127.0.0.1", alias="MIGRATOR_HOST")
    migrator_port: int = Field(default=8000, alias="MIGRATOR_PORT")
    migrator_match_concurrency: int = Field(default=4, alias="MIGRATOR_MATCH_CONCURRENCY")
    migrator_match_threshold_accept: float = Field(default=0.7, alias="MIGRATOR_MATCH_THRESHOLD_ACCEPT")
    migrator_match_threshold_review: float = Field(default=0.4, alias="MIGRATOR_MATCH_THRESHOLD_REVIEW")
    migrator_log_level: str = Field(default="INFO", alias="MIGRATOR_LOG_LEVEL")


class DataSection(BaseModel):
    data_dir: str = ".migrator"
    db_path: str = ".migrator/migrator.db"


class ServerSection(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000


class MatchingSection(BaseModel):
    concurrency: int = 4
    threshold_accept: float = 0.7
    threshold_review: float = 0.4
    duration_tolerance_strict_seconds: int = 3
    duration_tolerance_loose_seconds: int = 10
    search_top_k: int = 5


class ImportingSection(BaseModel):
    batch_size: int = 50
    rate_limit_backoff_seconds: int = 30
    rate_limit_max_retries: int = 3
    default_privacy: str = "PRIVATE"


class LoggingSection(BaseModel):
    level: str = "INFO"
    json_path: str = "logs/migrator.log"


class TomlSettings(BaseModel):
    """Non-secret settings loaded from config.toml (and optional config.local.toml)."""

    data: DataSection = DataSection()
    server: ServerSection = ServerSection()
    matching: MatchingSection = MatchingSection()
    importing: ImportingSection = ImportingSection()
    logging: LoggingSection = LoggingSection()


def _parse_toml_text(text: str) -> dict[str, Any]:
    """Tiny TOML parser for the subset we use.

    Avoids hard-depending on `tomllib` (stdlib only on 3.11+, but we want this
    code to be readable). Sections, key=value pairs, ints/floats/bools/strings,
    `#` comments, dotted sections like `[matching]`.
    """
    import re

    out: dict[str, Any] = {}
    stack: list[dict[str, Any]] = [out]
    section_re = re.compile(r"^\s*\[([^\]]+)\]\s*$")
    kv_re = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$")
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        m_sec = section_re.match(line)
        if m_sec:
            section = m_sec.group(1)
            target: dict[str, Any] = out
            for p in section.split("."):
                target = target.setdefault(p, {})  # type: ignore[index]
            stack.append(target)
            continue
        m_kv = kv_re.match(line)
        if m_kv:
            key, raw_val = m_kv.group(1), m_kv.group(2)
            val: Any
            if raw_val.startswith('"') and raw_val.endswith('"'):
                val = raw_val[1:-1]
            elif raw_val.lower() in {"true", "false"}:
                val = raw_val.lower() == "true"
            else:
                try:
                    val = int(raw_val)
                except ValueError:
                    try:
                        val = float(raw_val)
                    except ValueError:
                        val = raw_val
            stack[-1][key] = val
    return out


def _load_toml() -> TomlSettings:
    parsed: dict[str, Any] = {}
    for path in (PROJECT_ROOT / "config.toml", PROJECT_ROOT / "config.local.toml"):
        if path.exists():
            data = _parse_toml_text(path.read_text(encoding="utf-8"))
            for k, v in data.items():
                if isinstance(v, dict) and isinstance(parsed.get(k), dict):
                    parsed[k].update(v)
                else:
                    parsed[k] = v
    return TomlSettings.model_validate(parsed) if parsed else TomlSettings()


@lru_cache(maxsize=1)
def get_env() -> EnvSettings:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    return EnvSettings()


@lru_cache(maxsize=1)
def get_toml() -> TomlSettings:
    return _load_toml()


class Settings:
    """Combined settings object used everywhere else in the app."""

    def __init__(self, env: EnvSettings | None = None, toml: TomlSettings | None = None) -> None:
        self.env = env or get_env()
        self.toml = toml or get_toml()

    @property
    def data_dir(self) -> Path:
        return _resolve(self.env.migrator_data_dir or self.toml.data.data_dir)

    @property
    def db_path(self) -> Path:
        return _resolve(self.env.migrator_db_path or self.toml.data.db_path)

    @property
    def host(self) -> str:
        return self.env.migrator_host or self.toml.server.host

    @property
    def port(self) -> int:
        return self.env.migrator_port or self.toml.server.port

    @property
    def concurrency(self) -> int:
        return self.env.migrator_match_concurrency or self.toml.matching.concurrency

    @property
    def threshold_accept(self) -> float:
        return self.env.migrator_match_threshold_accept or self.toml.matching.threshold_accept

    @property
    def threshold_review(self) -> float:
        return self.env.migrator_match_threshold_review or self.toml.matching.threshold_review

    @property
    def log_level(self) -> str:
        return self.env.migrator_log_level or self.toml.logging.level

    @property
    def log_json_path(self) -> Path:
        return _resolve(self.toml.logging.json_path)

    @property
    def ytm_headers_path(self) -> Path:
        return self.data_dir / "headers_auth.json"

    @property
    def spotify_token_path(self) -> Path:
        return self.data_dir / "spotify_token.bin"

    def ensure_data_dir(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_json_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_data_dir()
    return s


def reset_cache() -> None:
    """Clear cached settings (used by tests / after writing .env)."""
    get_env.cache_clear()
    get_toml.cache_clear()
    get_settings.cache_clear()