"""Tests for backend health endpoint and standardized API error envelopes."""

from httpx import AsyncClient


async def test_health_endpoint_returns_standardized_envelope(client: AsyncClient) -> None:
    """GET /api/v1/health should return success envelope with service metadata."""
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "data": {
            "service": "backend",
            "status": "ok",
            "version": "0.1.0",
            "environment": "test",
        },
    }


async def test_unknown_route_returns_standardized_error_envelope(client: AsyncClient) -> None:
    """Unknown endpoints should return standardized ApiErrorResponse without leaking internals."""
    response = await client.get("/api/v1/nonexistent")

    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "NOT_FOUND"
