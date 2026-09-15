"""增加问题当前判断与来源关系。"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.config import settings

revision = "20260916_12"
down_revision = "20260916_11"
branch_labels = None
depends_on = None


def _app_role() -> str:
    return op.get_bind().dialect.identifier_preparer.quote(settings.db_user)


def upgrade() -> None:
    app_role = _app_role()
    op.create_table(
        "issues",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
        sa.Column(
            "revision", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("creation_operation_key", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("btrim(description) <> ''", name="ck_issue_description"),
        sa.CheckConstraint(
            "decision IN ('modify', 'observe', 'reject')", name="ck_issue_decision"
        ),
        sa.CheckConstraint("btrim(reason) <> ''", name="ck_issue_reason"),
        sa.CheckConstraint("status IN ('open', 'closed')", name="ck_issue_status"),
        sa.CheckConstraint("revision > 0", name="ck_issue_revision_positive"),
        sa.ForeignKeyConstraint(
            ["work_id", "workspace_id"],
            ["works.id", "works.workspace_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "work_id", "creation_operation_key", name="uq_issue_work_operation"
        ),
    )
    op.alter_column("issues", "status", server_default=None)
    op.alter_column("issues", "revision", server_default=None)
    op.create_index(
        "ix_issues_work_updated",
        "issues",
        ["workspace_id", "work_id", sa.text("updated_at DESC"), sa.text("id DESC")],
    )
    op.create_table(
        "issue_evidence_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("issue_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("observation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("feedback_submission_id", postgresql.UUID(as_uuid=True)),
        sa.CheckConstraint(
            "(observation_id IS NOT NULL AND feedback_submission_id IS NULL) "
            "OR (observation_id IS NULL AND feedback_submission_id IS NOT NULL)",
            name="ck_issue_evidence_link_source",
        ),
        sa.ForeignKeyConstraint(["issue_id"], ["issues.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["observation_id"], ["playtest_observations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["feedback_submission_id"],
            ["playtest_feedback_submissions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "issue_id", "observation_id", name="uq_issue_evidence_link_observation"
        ),
        sa.UniqueConstraint(
            "issue_id",
            "feedback_submission_id",
            name="uq_issue_evidence_link_feedback_submission",
        ),
    )
    op.create_index(
        "ix_issue_evidence_links_issue", "issue_evidence_links", ["issue_id", "id"]
    )

    op.execute(
        "GRANT SELECT, INSERT, UPDATE (description, decision, reason, status, revision, updated_at), DELETE "
        f"ON TABLE issues TO {app_role}"
    )
    op.execute(
        f"GRANT SELECT, INSERT, DELETE ON TABLE issue_evidence_links TO {app_role}"
    )
    for table in ("issues", "issue_evidence_links"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    op.execute(
        """
        CREATE FUNCTION public.issue_evidence_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(current_setting('app.issue_evidence_id', true), '')::uuid
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.issue_evidence_candidate_observation_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(
                current_setting('app.issue_evidence_candidate_observation_id', true), ''
            )::uuid
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.issue_evidence_candidate_feedback_submission_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(
                current_setting('app.issue_evidence_candidate_feedback_submission_id', true), ''
            )::uuid
        $$
        """
    )
    for function in (
        "issue_evidence_id",
        "issue_evidence_candidate_observation_id",
        "issue_evidence_candidate_feedback_submission_id",
    ):
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{function}() TO {app_role}")

    issue_scope = (
        "workspace_id = public.work_management_workspace_id() "
        "AND work_id = public.work_management_id()"
    )
    op.execute(f"CREATE POLICY issue_read ON issues FOR SELECT USING ({issue_scope})")
    op.execute(
        f"CREATE POLICY issue_insert ON issues FOR INSERT WITH CHECK ({issue_scope})"
    )
    op.execute(
        "CREATE POLICY issue_update ON issues FOR UPDATE "
        f"USING ({issue_scope}) WITH CHECK ({issue_scope})"
    )
    op.execute(
        "CREATE POLICY project_baseline_issue_read ON issues FOR SELECT "
        "USING (public.project_baseline_maintenance())"
    )
    op.execute(
        "CREATE POLICY project_baseline_issue_delete ON issues FOR DELETE "
        "USING (public.project_baseline_maintenance())"
    )

    link_work_scope = (
        "EXISTS (SELECT 1 FROM issues issue "
        "WHERE issue.id = issue_evidence_links.issue_id "
        "AND issue.workspace_id = public.work_management_workspace_id() "
        "AND issue.work_id = public.work_management_id())"
    )
    op.execute(
        "CREATE POLICY issue_evidence_link_read ON issue_evidence_links FOR SELECT "
        f"USING ({link_work_scope})"
    )
    op.execute(
        "CREATE POLICY issue_evidence_link_insert ON issue_evidence_links FOR INSERT "
        f"WITH CHECK ({link_work_scope})"
    )
    op.execute(
        "CREATE POLICY issue_evidence_link_delete ON issue_evidence_links FOR DELETE "
        f"USING ({link_work_scope})"
    )
    op.execute(
        "CREATE POLICY issue_evidence_link_scope_read ON issue_evidence_links FOR SELECT "
        "USING (issue_id = public.issue_evidence_id())"
    )
    op.execute(
        "CREATE POLICY project_baseline_issue_evidence_link_read "
        "ON issue_evidence_links FOR SELECT "
        "USING (public.project_baseline_maintenance())"
    )
    op.execute(
        "CREATE POLICY project_baseline_issue_evidence_link_delete "
        "ON issue_evidence_links FOR DELETE "
        "USING (public.project_baseline_maintenance())"
    )

    observation_candidate_scope = (
        "id = public.issue_evidence_candidate_observation_id() "
        "AND EXISTS (SELECT 1 FROM playtest_sessions session "
        "WHERE session.id = playtest_observations.session_id "
        "AND session.workspace_id = public.playtest_management_workspace_id() "
        "AND session.work_id = public.playtest_management_work_id())"
    )
    op.execute(
        "CREATE POLICY issue_evidence_observation_read ON playtest_observations FOR SELECT "
        "USING ("
        f"{observation_candidate_scope} OR EXISTS ("
        "SELECT 1 FROM issue_evidence_links link "
        "WHERE link.issue_id = public.issue_evidence_id() "
        "AND link.observation_id = playtest_observations.id))"
    )
    feedback_candidate_scope = (
        "id = public.issue_evidence_candidate_feedback_submission_id() "
        "AND EXISTS (SELECT 1 FROM playtest_sessions session "
        "WHERE session.id = playtest_feedback_submissions.session_id "
        "AND session.workspace_id = public.playtest_management_workspace_id() "
        "AND session.work_id = public.playtest_management_work_id())"
    )
    op.execute(
        "CREATE POLICY issue_evidence_feedback_submission_read "
        "ON playtest_feedback_submissions FOR SELECT "
        "USING (status = 'submitted' AND ("
        f"{feedback_candidate_scope} OR EXISTS ("
        "SELECT 1 FROM issue_evidence_links link "
        "WHERE link.issue_id = public.issue_evidence_id() "
        "AND link.feedback_submission_id = playtest_feedback_submissions.id)))"
    )
    op.execute(
        "CREATE POLICY issue_evidence_feedback_answer_read "
        "ON playtest_feedback_answers FOR SELECT "
        "USING (status = 'submitted' AND EXISTS ("
        "SELECT 1 FROM issue_evidence_links link "
        "WHERE link.issue_id = public.issue_evidence_id() "
        "AND link.feedback_submission_id = playtest_feedback_answers.submission_id))"
    )
    op.execute(
        "CREATE POLICY issue_evidence_feedback_item_read "
        "ON playtest_feedback_items FOR SELECT USING (EXISTS ("
        "SELECT 1 FROM playtest_feedback_answers answer "
        "JOIN issue_evidence_links link "
        "ON link.feedback_submission_id = answer.submission_id "
        "WHERE answer.item_id = playtest_feedback_items.id "
        "AND answer.status = 'submitted' "
        "AND link.issue_id = public.issue_evidence_id()))"
    )
    op.execute(
        "CREATE POLICY issue_evidence_feedback_option_read "
        "ON playtest_feedback_options FOR SELECT USING (EXISTS ("
        "SELECT 1 FROM playtest_feedback_answers answer "
        "JOIN issue_evidence_links link "
        "ON link.feedback_submission_id = answer.submission_id "
        "WHERE answer.option_id = playtest_feedback_options.id "
        "AND answer.status = 'submitted' "
        "AND link.issue_id = public.issue_evidence_id()))"
    )


def downgrade() -> None:
    for policy, table in (
        ("issue_evidence_feedback_option_read", "playtest_feedback_options"),
        ("issue_evidence_feedback_item_read", "playtest_feedback_items"),
        ("issue_evidence_feedback_answer_read", "playtest_feedback_answers"),
        (
            "issue_evidence_feedback_submission_read",
            "playtest_feedback_submissions",
        ),
        ("issue_evidence_observation_read", "playtest_observations"),
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    for policy in (
        "project_baseline_issue_evidence_link_delete",
        "project_baseline_issue_evidence_link_read",
        "issue_evidence_link_scope_read",
        "issue_evidence_link_delete",
        "issue_evidence_link_insert",
        "issue_evidence_link_read",
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON issue_evidence_links")
    for policy in (
        "project_baseline_issue_delete",
        "project_baseline_issue_read",
        "issue_update",
        "issue_insert",
        "issue_read",
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON issues")
    op.drop_index("ix_issue_evidence_links_issue", table_name="issue_evidence_links")
    op.drop_table("issue_evidence_links")
    op.drop_index("ix_issues_work_updated", table_name="issues")
    op.drop_table("issues")
    for function in (
        "issue_evidence_candidate_feedback_submission_id",
        "issue_evidence_candidate_observation_id",
        "issue_evidence_id",
    ):
        op.execute(f"DROP FUNCTION public.{function}()")
