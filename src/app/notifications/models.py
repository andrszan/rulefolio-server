from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MailOutbox(Base):
    __tablename__ = "mail_outbox"
    __table_args__ = (
        CheckConstraint(
            "(credential_id IS NOT NULL AND business_scope IS NULL "
            "AND frozen_subject IS NULL AND frozen_body IS NULL) OR "
            "(credential_id IS NULL AND btrim(business_scope) <> '' "
            "AND btrim(frozen_subject) <> '' AND btrim(frozen_body) <> '')",
            name="ck_mail_outbox_delivery_shape",
        ),
        CheckConstraint(
            "retry_operation_key IS NULL OR credential_id IS NULL",
            name="ck_mail_outbox_retry_business_only",
        ),
        Index(
            "uq_mail_outbox_credential",
            "credential_id",
            unique=True,
            postgresql_where=text("credential_id IS NOT NULL"),
        ),
        Index(
            "uq_mail_outbox_todo_retry_operation",
            "todo_id",
            "retry_operation_key",
            unique=True,
            postgresql_where=text("retry_operation_key IS NOT NULL"),
        ),
        Index("ix_mail_outbox_todo_created", "todo_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    credential_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("identity_one_time_credentials.id", ondelete="CASCADE"),
    )
    todo_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("notification_todos.id", ondelete="SET NULL"),
    )
    retry_operation_key: Mapped[str | None] = mapped_column(String(128))
    recipient_account_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("identity_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    workspace_name: Mapped[str | None] = mapped_column(String(160))
    business_scope: Mapped[str | None] = mapped_column(String(128))
    frozen_subject: Mapped[str | None] = mapped_column(String(200))
    frozen_body: Mapped[str | None] = mapped_column(String(4_000))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    claim_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    token_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    token_nonce: Mapped[bytes | None] = mapped_column(LargeBinary(12))
    key_version: Mapped[int | None] = mapped_column(Integer)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    dispatch_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    smtp_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class NotificationTodo(Base):
    __tablename__ = "notification_todos"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('workspace_invitation', 'playtest_invitation', "
            "'playtest_arrangement_updated', 'playtest_material_updated', "
            "'playtest_cancelled', 'feedback_submitted', 'issue_opened', "
            "'retest_arrangement_needed', 'retest_arranged')",
            name="ck_notification_todo_kind",
        ),
        CheckConstraint(
            "target_kind IN ('workspace_invitation', 'playtest_session', "
            "'feedback_submission', 'issue', 'issue_retest')",
            name="ck_notification_todo_target_kind",
        ),
        CheckConstraint(
            "status IN ('open', 'completed', 'cancelled')",
            name="ck_notification_todo_status",
        ),
        CheckConstraint(
            "btrim(source_key) <> ''", name="ck_notification_todo_source_key"
        ),
        CheckConstraint("btrim(summary) <> ''", name="ck_notification_todo_summary"),
        CheckConstraint(
            "btrim(context_label) <> ''", name="ck_notification_todo_context_label"
        ),
        UniqueConstraint(
            "recipient_account_id",
            "kind",
            "source_key",
            name="uq_notification_todo_recipient_kind_source",
        ),
        Index(
            "ix_notification_todo_recipient_status_created",
            "recipient_account_id",
            "status",
            "created_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    recipient_account_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("identity_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    work_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("works.id", ondelete="CASCADE"),
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_key: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(String(200), nullable=False)
    context_label: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    retry_used: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
