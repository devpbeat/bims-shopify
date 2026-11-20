"""Application settings, loaded from environment variables / .env file."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./bims_shopify.db"
    admin_token: str = "change-me"
    fernet_key: str = ""
    auto_import_tick_minutes: int = 15
    log_level: str = "INFO"
    environment: str = "development"

    shopify_api_key: str = ""
    shopify_api_secret: str = ""
    #: Public origin of THIS deployment. It is not cosmetic: it builds the
    #: Shopify OAuth ``redirect_uri`` and the buyer-facing Bancard vPOS payment
    #: links. A stale value here mints links to a dead host and buyers silently
    #: cannot pay, so the default is the local dev origin rather than any real
    #: domain -- a deployment that forgets to set it breaks loudly in testing
    #: instead of quietly in production.
    public_base_url: str = "http://localhost:8000"

    # Signing secret for merchant-portal login session JWTs (see
    # api/portal_auth.py). Left blank by default so it can be *derived*
    # from `fernet_key` (already a per-deployment secret) rather than
    # forcing yet another env var everywhere; set it explicitly to rotate
    # session signing independently of the encryption key.
    portal_session_secret: str = ""

    # Signing secret for the buyer-facing payment links (checkout_link.py).
    # Blank means "derive from fernet_key", like the portal session secret.
    checkout_link_secret: str = ""


def get_settings() -> Settings:
    return Settings()
