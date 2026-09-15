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
        Index(
            "uq_mail_outbox_credential",
            "credential_id",
            unique=True,
            postgresql_where=text("credential_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    credential_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("identity_one_time_credentials.id", ondelete="CASCADE"),
    )
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
