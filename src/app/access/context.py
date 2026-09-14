from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


def set_actor(session: Session, account_id: UUID) -> None:
    session.execute(
        text("SELECT set_config('app.actor_id', :account_id, true)"),
        {"account_id": str(account_id)},
    )
