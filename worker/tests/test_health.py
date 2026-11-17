"""Tests for Python Worker health endpoints."""

from httpx import AsyncClient


async def test_worker_health_endpoints(worker_client: AsyncClient) -> None:
    """Worker exposes /health and /api/v1/health with standardized envelope."""
    for path in ("/health", "/api/v1/health"):
        response = await worker_client.get(path)
        assert response.status_code == 200
        assert response.json() == {
            "success": True,
            "data": {
                "service": "worker",
                "status": "ok",
                "version": "0.1.0",
                "environment": "test",
            },
        }
