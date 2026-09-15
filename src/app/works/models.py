from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
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


class Work(Base):
    __tablename__ = "works"
    __table_args__ = (
        CheckConstraint("min_players > 0", name="ck_work_min_players_positive"),
        CheckConstraint("max_players >= min_players", name="ck_work_player_range"),
        CheckConstraint(
            "estimated_duration_minutes > 0",
            name="ck_work_estimated_duration_positive",
        ),
        CheckConstraint("revision > 0", name="ck_work_revision_positive"),
        CheckConstraint(
            "(rule_name IS NULL AND rule_description IS NULL AND rule_content IS NULL) "
            "OR (btrim(rule_name) <> '' AND btrim(rule_content) <> '')",
            name="ck_work_current_rule_complete",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_work_id_workspace"),
        Index("ix_works_workspace_created", "workspace_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    creative_stage: Mapped[str] = mapped_column(String(160), nullable=False)
    target_experience: Mapped[str] = mapped_column(Text, nullable=False)
    min_players: Mapped[int] = mapped_column(Integer, nullable=False)
    max_players: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    rule_name: Mapped[str | None] = mapped_column(String(160))
    rule_description: Mapped[str | None] = mapped_column(Text)
    rule_content: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkMaterialFile(Base):
    __tablename__ = "work_material_files"

    work_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("works.id", ondelete="CASCADE"),
        primary_key=True,
    )
    file_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("files.id", ondelete="RESTRICT"),
        primary_key=True,
    )
