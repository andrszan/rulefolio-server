from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
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


class PlaytestFeedbackItem(Base):
    __tablename__ = "playtest_feedback_items"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('short_text', 'single_choice', 'number')",
            name="ck_playtest_feedback_item_kind",
        ),
        CheckConstraint(
            "btrim(question) <> ''", name="ck_playtest_feedback_item_question"
        ),
        CheckConstraint("revision > 0", name="ck_playtest_feedback_item_revision"),
        UniqueConstraint("id", "session_id", name="uq_playtest_feedback_item_scope"),
        UniqueConstraint(
            "session_id",
            "creation_operation_key",
            name="uq_playtest_feedback_item_operation",
        ),
        Index(
            "ix_playtest_feedback_items_session_created",
            "session_id",
            "created_at",
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
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_locked: Mapped[bool] = mapped_column(nullable=False, default=False)
    creation_operation_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PlaytestFeedbackOption(Base):
    __tablename__ = "playtest_feedback_options"
    __table_args__ = (
        CheckConstraint("btrim(label) <> ''", name="ck_playtest_feedback_option_label"),
        CheckConstraint("position > 0", name="ck_playtest_feedback_option_position"),
        ForeignKeyConstraint(
            ["item_id", "session_id"],
            ["playtest_feedback_items.id", "playtest_feedback_items.session_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "id", "item_id", "session_id", name="uq_playtest_feedback_option_scope"
        ),
        UniqueConstraint(
            "item_id", "position", name="uq_playtest_feedback_option_position"
        ),
        Index(
            "ix_playtest_feedback_options_item_position", "item_id", "position", "id"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    session_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    item_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class PlaytestFeedbackSubmission(Base):
    __tablename__ = "playtest_feedback_submissions"
    __table_args__ = (
        CheckConstraint(
            "source IN ('direct', 'oral_discussion', 'paper_record', "
            "'organizer_observation', 'temporary_alias')",
            name="ck_playtest_feedback_submission_source",
        ),
        CheckConstraint(
            "status IN ('draft', 'submitted')",
            name="ck_playtest_feedback_submission_status",
        ),
        CheckConstraint(
            "revision > 0", name="ck_playtest_feedback_submission_revision"
        ),
        CheckConstraint(
            "(source = 'direct' "
            "AND direct_author_account_id IS NOT NULL "
            "AND recorded_by_account_id = direct_author_account_id) "
            "OR (source <> 'direct' "
            "AND direct_author_account_id IS NULL "
            "AND status = 'submitted')",
            name="ck_playtest_feedback_submission_author",
        ),
        CheckConstraint(
            "(source = 'temporary_alias' AND btrim(temporary_alias) <> '') "
            "OR (source <> 'temporary_alias' AND temporary_alias IS NULL)",
            name="ck_playtest_feedback_submission_alias",
        ),
        UniqueConstraint(
            "id", "session_id", name="uq_playtest_feedback_submission_scope"
        ),
        UniqueConstraint(
            "session_id",
            "direct_author_account_id",
            name="uq_playtest_feedback_submission_direct_author",
        ),
        UniqueConstraint(
            "session_id",
            "creation_operation_key",
            name="uq_playtest_feedback_submission_operation",
        ),
        Index(
            "ix_playtest_feedback_submissions_session_updated",
            "session_id",
            "updated_at",
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
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    direct_author_account_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("identity_accounts.id", ondelete="RESTRICT"),
    )
    temporary_alias: Mapped[str | None] = mapped_column(String(160))
    recorded_by_account_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("identity_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    creation_operation_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PlaytestFeedbackAnswer(Base):
    __tablename__ = "playtest_feedback_answers"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('short_text', 'single_choice', 'number')",
            name="ck_playtest_feedback_answer_kind",
        ),
        CheckConstraint(
            "(kind = 'short_text' AND btrim(text_value) <> '' "
            "AND option_id IS NULL AND number_value IS NULL) "
            "OR (kind = 'single_choice' AND text_value IS NULL "
            "AND option_id IS NOT NULL AND number_value IS NULL) "
            "OR (kind = 'number' AND text_value IS NULL "
            "AND option_id IS NULL AND number_value IS NOT NULL "
            "AND number_value NOT IN ('NaN'::double precision, 'Infinity'::double precision, '-Infinity'::double precision))",
            name="ck_playtest_feedback_answer_value",
        ),
        CheckConstraint(
            "source IN ('direct', 'oral_discussion', 'paper_record', "
            "'organizer_observation', 'temporary_alias')",
            name="ck_playtest_feedback_answer_source",
        ),
        CheckConstraint(
            "status IN ('draft', 'submitted')",
            name="ck_playtest_feedback_answer_status",
        ),
        CheckConstraint(
            "(source = 'direct' "
            "AND direct_author_account_id IS NOT NULL "
            "AND recorded_by_account_id = direct_author_account_id) "
            "OR (source <> 'direct' "
            "AND direct_author_account_id IS NULL "
            "AND status = 'submitted')",
            name="ck_playtest_feedback_answer_author",
        ),
        ForeignKeyConstraint(
            ["submission_id", "session_id"],
            [
                "playtest_feedback_submissions.id",
                "playtest_feedback_submissions.session_id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["item_id", "session_id"],
            ["playtest_feedback_items.id", "playtest_feedback_items.session_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["option_id", "item_id", "session_id"],
            [
                "playtest_feedback_options.id",
                "playtest_feedback_options.item_id",
                "playtest_feedback_options.session_id",
            ],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "submission_id", "item_id", name="uq_playtest_feedback_answer_item"
        ),
        Index(
            "ix_playtest_feedback_answers_session_item", "session_id", "item_id", "id"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    submission_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    session_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    item_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    text_value: Mapped[str | None] = mapped_column(Text)
    option_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    number_value: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    direct_author_account_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("identity_accounts.id", ondelete="RESTRICT"),
    )
    recorded_by_account_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("identity_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
