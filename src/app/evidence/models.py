from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PlaytestObservation(Base):
    __tablename__ = "playtest_observations"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('fact', 'organizer_interpretation', 'temporary_variant')",
            name="ck_playtest_observation_kind",
        ),
        CheckConstraint("btrim(content) <> ''", name="ck_playtest_observation_content"),
        Index(
            "ix_playtest_observations_session_kind_recorded",
            "session_id",
            "kind",
            "recorded_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    session_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("playtest_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_by_account_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("identity_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
