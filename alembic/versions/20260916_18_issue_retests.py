"""增加问题复测关系与当前结论。"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.config import settings

revision = "20260916_18"
down_revision = "20260916_17"
branch_labels = None
depends_on = None


def _role(name: str) -> str:
    return op.get_bind().dialect.identifier_preparer.quote(name)


def upgrade() -> None:
    app_role = _role(settings.db_user)
    op.add_column("issues", sa.Column("adjustment_note", sa.Text()))
    op.create_table(
        "issue_retest_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("issue_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conclusion", sa.String(length=32)),
        sa.Column("conclusion_reason", sa.Text()),
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
        sa.CheckConstraint(
            "(conclusion IS NULL AND conclusion_reason IS NULL) OR "
            "(conclusion IN ('verified', 'continue_observing', 'adjust_again', "
            "'insufficient_evidence') AND btrim(conclusion_reason) <> '')",
            name="ck_issue_retest_link_conclusion",
        ),
        sa.ForeignKeyConstraint(["issue_id"], ["issues.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["playtest_sessions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "issue_id", "session_id", name="uq_issue_retest_link_session"
        ),
    )
    op.create_index(
        "ix_issue_retest_links_issue", "issue_retest_links", ["issue_id", "id"]
    )
    op.create_index(
        "uq_issue_retest_link_current_conclusion",
        "issue_retest_links",
        ["issue_id"],
        unique=True,
        postgresql_where=sa.text("conclusion IS NOT NULL"),
    )

    op.execute(f"GRANT UPDATE (adjustment_note) ON TABLE issues TO {app_role}")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE (conclusion, conclusion_reason, updated_at), DELETE "
        f"ON TABLE issue_retest_links TO {app_role}"
    )
    op.execute("ALTER TABLE issue_retest_links ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE issue_retest_links FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE FUNCTION public.issue_retest_issue_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(current_setting('app.issue_retest_issue_id', true), '')::uuid
        $$
        """
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.issue_retest_issue_id() TO {app_role}"
    )
    retest_scope = "issue_id = public.issue_retest_issue_id()"
    op.execute(
        "CREATE POLICY issue_retest_link_read ON issue_retest_links FOR SELECT "
        f"USING ({retest_scope})"
    )
    op.execute(
        "CREATE POLICY issue_retest_link_insert ON issue_retest_links FOR INSERT "
        f"WITH CHECK ({retest_scope})"
    )
    op.execute(
        "CREATE POLICY issue_retest_link_update ON issue_retest_links FOR UPDATE "
        f"USING ({retest_scope}) WITH CHECK ({retest_scope})"
    )
    op.execute(
        "CREATE POLICY project_baseline_issue_retest_link_read "
        "ON issue_retest_links FOR SELECT "
        "USING (public.project_baseline_maintenance())"
    )
    op.execute(
        "CREATE POLICY project_baseline_issue_retest_link_delete "
        "ON issue_retest_links FOR DELETE "
        "USING (public.project_baseline_maintenance())"
    )


def downgrade() -> None:
    for policy in (
        "project_baseline_issue_retest_link_delete",
        "project_baseline_issue_retest_link_read",
        "issue_retest_link_update",
        "issue_retest_link_insert",
        "issue_retest_link_read",
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON issue_retest_links")
    op.execute("DROP FUNCTION public.issue_retest_issue_id()")
    op.drop_index(
        "uq_issue_retest_link_current_conclusion", table_name="issue_retest_links"
    )
    op.drop_index("ix_issue_retest_links_issue", table_name="issue_retest_links")
    op.drop_table("issue_retest_links")
    op.drop_column("issues", "adjustment_note")
