from fastapi import FastAPI

from app.api import router as api_router
from app.core.config import settings
from app.core.database import lifespan
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import register_middlewares
from app.health import router as health_router

configure_logging(settings.log_level)
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    docs_url=f"{settings.api_prefix}/docs" if settings.enable_api_docs else None,
    redoc_url=f"{settings.api_prefix}/redoc" if settings.enable_api_docs else None,
    openapi_url=f"{settings.api_prefix}/openapi.json"
    if settings.enable_api_docs
    else None,
)
register_exception_handlers(app)
register_middlewares(app, settings.cors_origins)
app.include_router(health_router)
app.include_router(api_router, prefix=settings.api_prefix)
