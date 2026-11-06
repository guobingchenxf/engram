from __future__ import annotations

import secrets

from fastapi.testclient import TestClient

from engram.api import create_app
from engram.config import MemoryConfig


def test_api_flow(config: MemoryConfig) -> None:
    client = TestClient(create_app(config))

    created = client.post(
        "/v1/memories",
        json={"user_id": "u1", "content": "I prefer Chinese explanations."},
    )
    assert created.status_code == 200
    memory_id = created.json()["id"]

    search = client.post(
        "/v1/memories/search",
        json={"user_id": "u1", "query": "Chinese explanations", "top_k": 5, "token_budget": 200},
    )
    assert search.status_code == 200
    assert search.json()["hits"]

    stats = client.get("/v1/stats", params={"user_id": "u1"})
    assert stats.status_code == 200
    assert stats.json()["total"] == 1

    deleted = client.delete(f"/v1/memories/{memory_id}")
    assert deleted.status_code == 204


def test_api_rejects_secret(config: MemoryConfig) -> None:
    client = TestClient(create_app(config))
    response = client.post(
        "/v1/memories",
        json={"user_id": "u1", "content": "My API key is sk-abcdefghijklmnop123456."},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "safety_violation"


def test_api_explain(config: MemoryConfig) -> None:
    client = TestClient(create_app(config))
    client.post(
        "/v1/memories",
        json={"user_id": "u1", "content": "The user prefers Chinese explanations."},
    )
    response = client.post(
        "/v1/explain",
        json={"user_id": "u1", "query": "Chinese explanations"},
    )
    assert response.status_code == 200
    assert response.json()["base_answer"]
    assert response.json()["influences"]

    view = client.get(
        "/v1/explain/view",
        params={"user_id": "u1", "query": "Chinese explanations"},
    )
    assert view.status_code == 200
    assert "Engram memory influence" in view.text


def test_api_key_auth(config: MemoryConfig) -> None:
    test_key = secrets.token_urlsafe(16)
    secured = config.with_overrides(api_key=test_key)
    client = TestClient(create_app(secured))

    assert client.get("/health").status_code == 200
    unauthenticated = client.post(
        "/v1/memories",
        json={"user_id": "u1", "content": "I prefer Chinese explanations."},
    )
    assert unauthenticated.status_code == 401

    authenticated = client.post(
        "/v1/memories",
        headers={"X-API-Key": test_key},
        json={"user_id": "u1", "content": "I prefer Chinese explanations."},
    )
    assert authenticated.status_code == 200
