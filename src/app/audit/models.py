from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SecurityAudit(Base):
    __tablename__ = "security_audits"

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_account_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    target_account_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    operator: Mapped[str | None] = mapped_column(String(128))
    reason: Mapped[str | None] = mapped_column(String(256))
    scope: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
