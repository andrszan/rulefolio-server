from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.recovery.models import RecoveryState


def _allowed(session: Session, stages: set[str]) -> bool:
    if settings.recovery_deployment_frozen:
        return False
    try:
        stage = session.scalar(select(RecoveryState.stage).where(RecoveryState.id == 1))
        return stage in stages
    except SQLAlchemyError:
        session.rollback()
        return False


def api_requests_allowed(session: Session) -> bool:
    return _allowed(session, {"api_open", "open"})


def dispatcher_allowed(session: Session) -> bool:
    return _allowed(session, {"open"})
