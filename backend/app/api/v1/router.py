"""Root v1 API router."""

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.credits import credits_router
from app.api.v1.documents import documents_router, jobs_router
from app.api.v1.health import router as health_router
from app.api.v1.workspaces import invitations_router, workspaces_router

api_v1_router = APIRouter()
api_v1_router.include_router(health_router)
api_v1_router.include_router(auth_router)
api_v1_router.include_router(workspaces_router)
api_v1_router.include_router(invitations_router)
api_v1_router.include_router(credits_router)
api_v1_router.include_router(documents_router)
api_v1_router.include_router(jobs_router)
