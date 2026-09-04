"""Environment-driven configuration.

Secrets are deliberately *not* stored in Mongo. A tenant document names the
environment variable holding its DataCite (Fabrica) password via
``datacite.password_env``; the value is read from the process environment at
publication time. That keeps a database dump from being a credential leak.
"""

import functools
import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IGSN_", extra="ignore")

    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "igsn"

    # Bearer token for the admin API (tenant CRUD, vocabulary edits, counter seeding).
    # Empty disables the admin API entirely rather than leaving it open.
    admin_token: str = ""

    # Public base URL of this service, used to build resolver URLs registered with
    # DataCite for records that carry no tenant landing page.
    public_url: str = "http://localhost:8000"

    # Master switch for every outbound *write* to DataCite. False means no code
    # path can create or update a DOI: publish requests are refused with 503
    # instead of queueing silently, and the drain loop claims nothing. This is a
    # policy flag (may we spend money at DataCite?), separate from
    # publisher_enabled below, which is a topology flag (does *this* process run
    # the drain loop?). Reads -- datacite.fetch(), used by `sync-status` -- stay
    # available, because learning what DataCite already has is free and is a
    # prerequisite for turning publication on safely.
    datacite_enabled: bool = True

    # Background publisher loop.
    publisher_enabled: bool = True
    publisher_poll_seconds: float = 5.0
    publisher_max_attempts: int = 8
    publisher_backoff_base_seconds: float = 30.0

    datacite_timeout_seconds: float = 30.0

    log_level: str = "INFO"


@functools.lru_cache
def get_settings() -> Settings:
    return Settings()


def datacite_password(password_env: str) -> str | None:
    """Resolve a tenant's DataCite password from the environment."""
    if not password_env:
        return None
    return os.environ.get(password_env) or None
