"""Tests for backend health endpoint and standardized API error envelopes."""

from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app


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


async def test_unhandled_500_includes_cors_headers_only_for_allowed_origins(
    test_settings: Settings,
) -> None:
    """500 errors must include CORS headers for allowed origins."""
    app = create_app(test_settings)

    @app.get("/api/v1/_test-crash")
    async def crash_endpoint() -> None:
        raise RuntimeError("Simulated database crash")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        allowed_res = await ac.get(
            "/api/v1/_test-crash",
            headers={"Origin": "http://localhost:3000"},
        )
        assert allowed_res.status_code == 500
        assert allowed_res.json()["error"]["code"] == "INTERNAL_ERROR"
        assert allowed_res.headers.get("access-control-allow-origin") == "http://localhost:3000"
        assert allowed_res.headers.get("access-control-allow-credentials") == "true"
        assert "Origin" in allowed_res.headers.get("vary", "")

        blocked_res = await ac.get(
            "/api/v1/_test-crash",
            headers={"Origin": "https://evil.example.com"},
        )
        assert blocked_res.status_code == 500
        assert blocked_res.json()["error"]["code"] == "INTERNAL_ERROR"
        assert blocked_res.headers.get("access-control-allow-origin") is None
