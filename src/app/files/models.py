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
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class StoredFile(Base):
    __tablename__ = "files"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'ready', 'failed')", name="ck_file_status"
        ),
        CheckConstraint("size_bytes >= 0", name="ck_file_size_nonnegative"),
        CheckConstraint("kind IN ('image', 'material')", name="ck_file_kind"),
        ForeignKeyConstraint(
            ["work_id", "workspace_id"],
            ["works.id", "works.workspace_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("object_key", name="uq_file_object_key"),
        Index("ix_files_work_status_created", "work_id", "status", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    work_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    uploader_account_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("identity_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    declared_content_type: Mapped[str] = mapped_column(String(127), nullable=False)
    detected_content_type: Mapped[str] = mapped_column(String(127), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    object_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="image")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
