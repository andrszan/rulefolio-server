from fastapi import APIRouter

from app.files.router import router as files_router
from app.identity.router import router as identity_router
from app.playtests.router import router as playtests_router
from app.works.router import router as works_router
from app.workspaces.router import router as workspaces_router

router = APIRouter()
router.include_router(identity_router)
router.include_router(files_router)
router.include_router(playtests_router)
router.include_router(workspaces_router)
router.include_router(works_router)
