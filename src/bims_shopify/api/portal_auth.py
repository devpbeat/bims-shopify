"""Password hashing, session-JWT signing, and login rate limiting for the portal.

Kept separate from ``api/portal.py`` and ``api/tenants.py`` (both of which
use it) so the security-sensitive primitives — hashing, signing, rate
limiting — live in one small, easily auditable module. Nothing here ever
logs a plaintext password.
"""
from __future__ import annotations

import hashlib
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from bims_shopify.config import Settings

SESSION_TOKEN_ALGORITHM = "HS256"
SESSION_TOKEN_TTL = timedelta(hours=24)

LOGIN_RATE_LIMIT_MAX_ATTEMPTS = 5
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 60


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed/legacy hash: treat as a non-match rather than raising.
        return False


def get_portal_session_secret(settings: Settings) -> str:
    """Resolve the JWT signing secret.

    Uses `settings.portal_session_secret` when explicitly set; otherwise
    derives a stable secret from `settings.fernet_key` so a working
    deployment doesn't need a brand-new env var. Falls back to a fixed
    (insecure) string only when neither is configured, e.g. bare local dev.
    """
    if settings.portal_session_secret:
        return settings.portal_session_secret
    if settings.fernet_key:
        return hashlib.sha256(f"portal-session:{settings.fernet_key}".encode("utf-8")).hexdigest()
    return "insecure-dev-portal-secret-change-me"


def create_session_token(*, secret: str, tenant_id: int, user_id: int) -> str:
    now = datetime.now(UTC)
    payload = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "iat": now,
        "exp": now + SESSION_TOKEN_TTL,
    }
    return jwt.encode(payload, secret, algorithm=SESSION_TOKEN_ALGORITHM)


class SessionTokenError(Exception):
    """Raised when a portal session JWT is missing, malformed, or expired."""


def decode_session_token(*, secret: str, token: str) -> dict:
    try:
        return jwt.decode(token, secret, algorithms=[SESSION_TOKEN_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise SessionTokenError(str(exc)) from exc


class LoginRateLimiter:
    """Simple in-memory fixed-window limiter: N attempts per key per window.

    In-memory and per-process by design (see task requirements) — this is
    not meant to survive a restart or work across multiple replicas, just
    to blunt naive credential-stuffing against a single running instance.
    """

    def __init__(
        self,
        *,
        max_attempts: int = LOGIN_RATE_LIMIT_MAX_ATTEMPTS,
        window_seconds: float = LOGIN_RATE_LIMIT_WINDOW_SECONDS,
    ) -> None:
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str) -> bool:
        """Record an attempt for `key` and return whether it's within limits."""
        now = time.monotonic()
        cutoff = now - self._window_seconds
        attempts = [t for t in self._attempts[key] if t > cutoff]
        attempts.append(now)
        self._attempts[key] = attempts
        return len(attempts) <= self._max_attempts
