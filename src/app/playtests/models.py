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
        CheckConstraint(
            "actual_headcount IS NULL OR actual_headcount >= 0",
            name="ck_playtest_session_actual_headcount_nonnegative",
        ),
        CheckConstraint(
            "actual_duration_minutes IS NULL OR actual_duration_minutes >= 0",
            name="ck_playtest_session_actual_duration_nonnegative",
        ),
        CheckConstraint(
            "completion_status IS NULL OR completion_status IN ('completed', 'interrupted')",
            name="ck_playtest_session_completion_status",
        ),
        CheckConstraint(
            "(NOT actual_material_recorded "
            "AND actual_rule_name IS NULL "
            "AND actual_rule_description IS NULL "
            "AND actual_rule_content IS NULL "
            "AND actual_material_change_reason IS NULL) "
            "OR (actual_material_recorded "
            "AND btrim(actual_rule_name) <> '' "
            "AND btrim(actual_rule_content) <> '')",
            name="ck_playtest_session_actual_material_shape",
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
    actual_headcount: Mapped[int | None] = mapped_column(Integer)
    actual_duration_minutes: Mapped[int | None] = mapped_column(Integer)
    completion_status: Mapped[str | None] = mapped_column(String(16))
    actual_material_recorded: Mapped[bool] = mapped_column(
        nullable=False, default=False
    )
    actual_rule_name: Mapped[str | None] = mapped_column(String(160))
    actual_rule_description: Mapped[str | None] = mapped_column(Text)
    actual_rule_content: Mapped[str | None] = mapped_column(Text)
    actual_material_change_reason: Mapped[str | None] = mapped_column(Text)
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


class PlaytestSessionActualMaterial(Base):
    __tablename__ = "playtest_session_actual_materials"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "file_id", name="uq_playtest_session_actual_material"
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
    file_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("files.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sha256: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)


class PlaytestSessionActualParticipant(Base):
    __tablename__ = "playtest_session_actual_participants"
    __table_args__ = (
        CheckConstraint(
            "(planned_account_id IS NOT NULL AND temporary_code IS NULL) "
            "OR (planned_account_id IS NULL AND btrim(temporary_code) <> '')",
            name="ck_playtest_actual_participant_identity",
        ),
        ForeignKeyConstraint(
            ["session_id", "planned_account_id"],
            [
                "playtest_session_participants.session_id",
                "playtest_session_participants.account_id",
            ],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "session_id",
            "planned_account_id",
            name="uq_playtest_actual_participant_account",
        ),
        UniqueConstraint(
            "session_id",
            "temporary_code",
            name="uq_playtest_actual_participant_temporary_code",
        ),
        Index("ix_playtest_actual_participants_session", "session_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    session_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("playtest_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    planned_account_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True)
    )
    temporary_code: Mapped[str | None] = mapped_column(String(160))
    seat_or_faction: Mapped[str | None] = mapped_column(String(160))
    score_or_outcome: Mapped[str | None] = mapped_column(String(160))
