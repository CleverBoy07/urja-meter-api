"""Runtime configuration, read from environment variables (and an optional .env file).

Credentials are never hard-coded: the service refuses to start talking to the portal
until URJA_USERNAME / URJA_PASSWORD are provided.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines) so we don't need python-dotenv.

    Existing environment variables always win over the file.
    """
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    portal_url: str = "https://urja-ops.flockenergy.tech"
    username: str = ""
    password: str = ""

    # Our own API's access control. Empty = open (fine for local dev; see README).
    api_keys: frozenset[str] = field(default_factory=frozenset)

    # Portal politeness. The portal allows ~120 requests per 60 s window per session;
    # we stay comfortably below that so interactive traffic never trips it.
    portal_rate_per_minute: int = 90
    portal_concurrency: int = 4
    portal_timeout_s: float = 15.0
    portal_max_retries: int = 4

    # Freshness policy (seconds).
    inventory_ttl_s: int = 15 * 60  # meters / hierarchy / geo / DTs
    readings_recent_ttl_s: int = 10 * 60  # days that may still receive readings
    readings_closed_ttl_s: int = 24 * 3600  # days that are complete & immutable

    # Optional background crawl of every meter's readings, used for fleet insights.
    warm_readings: bool = True
    warm_rate_per_minute: int = 30

    db_path: str = "data/urja.sqlite"
    sync_on_startup: bool = True


def load_settings(env_file: Path | None = None) -> Settings:
    _load_dotenv(env_file or Path.cwd() / ".env")
    keys = {k.strip() for k in os.environ.get("URJA_API_KEYS", "").split(",") if k.strip()}
    return Settings(
        portal_url=os.environ.get("URJA_PORTAL_URL", Settings.portal_url).rstrip("/"),
        username=os.environ.get("URJA_USERNAME", ""),
        password=os.environ.get("URJA_PASSWORD", ""),
        api_keys=frozenset(keys),
        portal_rate_per_minute=_env_int("URJA_PORTAL_RATE_PER_MINUTE", Settings.portal_rate_per_minute),
        portal_concurrency=_env_int("URJA_PORTAL_CONCURRENCY", Settings.portal_concurrency),
        portal_timeout_s=_env_float("URJA_PORTAL_TIMEOUT_S", Settings.portal_timeout_s),
        portal_max_retries=_env_int("URJA_PORTAL_MAX_RETRIES", Settings.portal_max_retries),
        inventory_ttl_s=_env_int("URJA_INVENTORY_TTL_S", Settings.inventory_ttl_s),
        readings_recent_ttl_s=_env_int("URJA_READINGS_RECENT_TTL_S", Settings.readings_recent_ttl_s),
        readings_closed_ttl_s=_env_int("URJA_READINGS_CLOSED_TTL_S", Settings.readings_closed_ttl_s),
        warm_readings=_env_bool("URJA_WARM_READINGS", Settings.warm_readings),
        warm_rate_per_minute=_env_int("URJA_WARM_RATE_PER_MINUTE", Settings.warm_rate_per_minute),
        db_path=os.environ.get("URJA_DB_PATH", Settings.db_path),
        sync_on_startup=_env_bool("URJA_SYNC_ON_STARTUP", Settings.sync_on_startup),
    )
