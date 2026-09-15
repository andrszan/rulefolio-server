from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
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


class Issue(Base):
    __tablename__ = "issues"
    __table_args__ = (
        CheckConstraint("btrim(description) <> ''", name="ck_issue_description"),
        CheckConstraint(
            "decision IN ('modify', 'observe', 'reject')", name="ck_issue_decision"
        ),
        CheckConstraint("btrim(reason) <> ''", name="ck_issue_reason"),
        CheckConstraint("status IN ('open', 'closed')", name="ck_issue_status"),
        CheckConstraint("revision > 0", name="ck_issue_revision_positive"),
        ForeignKeyConstraint(
            ["work_id", "workspace_id"],
            ["works.id", "works.workspace_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "work_id", "creation_operation_key", name="uq_issue_work_operation"
        ),
        Index("ix_issues_work_updated", "workspace_id", "work_id", "updated_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    workspace_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    work_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    creation_operation_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IssueEvidenceLink(Base):
    __tablename__ = "issue_evidence_links"
    __table_args__ = (
        CheckConstraint(
            "(observation_id IS NOT NULL AND feedback_submission_id IS NULL) "
            "OR (observation_id IS NULL AND feedback_submission_id IS NOT NULL)",
            name="ck_issue_evidence_link_source",
        ),
        UniqueConstraint(
            "issue_id", "observation_id", name="uq_issue_evidence_link_observation"
        ),
        UniqueConstraint(
            "issue_id",
            "feedback_submission_id",
            name="uq_issue_evidence_link_feedback_submission",
        ),
        Index("ix_issue_evidence_links_issue", "issue_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    issue_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("issues.id", ondelete="CASCADE"),
        nullable=False,
    )
    observation_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("playtest_observations.id", ondelete="RESTRICT"),
    )
    feedback_submission_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("playtest_feedback_submissions.id", ondelete="RESTRICT"),
    )
