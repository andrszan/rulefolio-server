from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, LargeBinary, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class RecoveryState(Base):
    __tablename__ = "recovery_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_recovery_state_singleton"),
        CheckConstraint(
            "stage IN ('open', 'recovering', 'api_open')",
            name="ck_recovery_state_stage",
        ),
        CheckConstraint(
            "authorization_mode IS NULL OR authorization_mode = 'restricted'",
            name="ck_recovery_state_authorization_mode",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    stage: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    recovery_id: Mapped[str | None] = mapped_column(String(128))
    manifest_sha256: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    recovery_target_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    authorization_mode: Mapped[str | None] = mapped_column(String(16))
    restricted_access_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    operator: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
