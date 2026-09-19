"""Application settings, loaded from environment variables / .env file."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./bims_shopify.db"
    admin_token: str = "change-me"
    fernet_key: str = ""
    sync_interval_minutes: int = 30
    auto_import_tick_minutes: int = 15
    log_level: str = "INFO"
    environment: str = "development"

    shopify_api_key: str = ""
    shopify_api_secret: str = ""
    public_base_url: str = "https://mystoresync.ignitesolutions.click"

    # Signing secret for merchant-portal login session JWTs (see
    # api/portal_auth.py). Left blank by default so it can be *derived*
    # from `fernet_key` (already a per-deployment secret) rather than
    # forcing yet another env var everywhere; set it explicitly to rotate
    # session signing independently of the encryption key.
    portal_session_secret: str = ""


def get_settings() -> Settings:
    return Settings()
