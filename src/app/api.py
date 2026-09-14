from fastapi import APIRouter

from app.identity.router import router as identity_router

router = APIRouter()
router.include_router(identity_router)
