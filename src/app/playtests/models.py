from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PlaytestPlan(Base):
    __tablename__ = "playtest_plans"
    __table_args__ = (
        UniqueConstraint(
            "id", "workspace_id", "work_id", name="uq_playtest_plan_scope"
        ),
        Index(
            "ix_playtest_plans_work_created",
            "workspace_id",
            "work_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    work_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    observation_goals: Mapped[str] = mapped_column(Text, nullable=False)
    recording_method: Mapped[str] = mapped_column(Text, nullable=False)
    creator_account_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("identity_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PlaytestSession(Base):
    __tablename__ = "playtest_sessions"
    __table_args__ = (
        CheckConstraint("capacity > 0", name="ck_playtest_session_capacity_positive"),
        CheckConstraint(
            "status IN ('scheduled', 'started', 'cancelled')",
            name="ck_playtest_session_status",
        ),
        CheckConstraint("revision > 0", name="ck_playtest_session_revision_positive"),
        CheckConstraint(
            "(status = 'started' AND started_at IS NOT NULL) "
            "OR (status IN ('scheduled', 'cancelled') AND started_at IS NULL)",
            name="ck_playtest_session_started_at_state",
        ),
        ForeignKeyConstraint(
            ["plan_id", "workspace_id", "work_id"],
            [
                "playtest_plans.id",
                "playtest_plans.workspace_id",
                "playtest_plans.work_id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["work_id", "workspace_id"],
            ["works.id", "works.workspace_id"],
            ondelete="CASCADE",
        ),
        Index(
            "ix_playtest_sessions_plan_scheduled",
            "plan_id",
            "scheduled_at",
            "id",
        ),
        Index(
            "ix_playtest_sessions_work_scheduled",
            "workspace_id",
            "work_id",
            "scheduled_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    plan_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    work_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    location: Mapped[str] = mapped_column(String(240), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="scheduled")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observation_goals: Mapped[str] = mapped_column(Text, nullable=False)
    recording_method: Mapped[str] = mapped_column(Text, nullable=False)
    work_name: Mapped[str] = mapped_column(String(160), nullable=False)
    rule_name: Mapped[str] = mapped_column(String(160), nullable=False)
    rule_description: Mapped[str | None] = mapped_column(Text)
    rule_content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PlaytestSessionMaterial(Base):
    __tablename__ = "playtest_session_materials"
    __table_args__ = (
        UniqueConstraint("session_id", "file_id", name="uq_playtest_session_material"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    session_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("playtest_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    file_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("files.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sha256: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)


class PlaytestSessionParticipant(Base):
    __tablename__ = "playtest_session_participants"
    __table_args__ = (
        CheckConstraint(
            "status IN ('invited', 'confirmed')",
            name="ck_playtest_participant_status",
        ),
        UniqueConstraint(
            "session_id", "account_id", name="uq_playtest_session_participant"
        ),
        Index(
            "ix_playtest_participants_session_status",
            "session_id",
            "status",
            "account_id",
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
    account_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("identity_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="invited")
    latest_outbox_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("mail_outbox.id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
