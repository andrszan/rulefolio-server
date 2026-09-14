from fastapi import APIRouter

from app.identity.router import router as identity_router
from app.workspaces.router import router as workspaces_router

router = APIRouter()
router.include_router(identity_router)
router.include_router(workspaces_router)
