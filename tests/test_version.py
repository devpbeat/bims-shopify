"""Tests for the public GET /version endpoint (running commit SHA)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from bims_shopify.api import health


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(health.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_version_returns_sha_from_env(client, monkeypatch):
    monkeypatch.setenv("GIT_SHA", "abc123def456")
    monkeypatch.setenv("BUILD_TIME", "2026-10-13T12:00:00Z")
    async with client:
        response = await client.get("/version")
    assert response.status_code == 200
    assert response.json() == {"sha": "abc123def456", "built_at": "2026-10-13T12:00:00Z"}


async def test_version_is_unknown_when_env_unset(client, monkeypatch):
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("BUILD_TIME", raising=False)
    async with client:
        response = await client.get("/version")
    assert response.status_code == 200
    assert response.json() == {"sha": "unknown", "built_at": "unknown"}


async def test_version_treats_blank_env_as_unknown(client, monkeypatch):
    monkeypatch.setenv("GIT_SHA", "  ")
    async with client:
        response = await client.get("/version")
    assert response.json()["sha"] == "unknown"


async def test_version_requires_no_auth(client, monkeypatch):
    monkeypatch.setenv("GIT_SHA", "abc123")
    async with client:
        response = await client.get("/version")  # no Authorization header, no cookies
    assert response.status_code == 200


async def test_version_exposes_nothing_beyond_sha_and_build_time(client, monkeypatch):
    monkeypatch.setenv("GIT_SHA", "abc123")
    monkeypatch.setenv("SHOPIFY_API_SECRET", "super-secret")
    async with client:
        body = (await client.get("/version")).json()
    assert set(body) == {"sha", "built_at"}
    assert "super-secret" not in str(body)


def test_version_is_registered_on_the_real_app():
    from bims_shopify.main import app

    assert "/version" in app.openapi()["paths"]
