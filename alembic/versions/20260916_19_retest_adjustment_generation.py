"""修正复测调整代际与作品范围策略。"""

import sqlalchemy as sa

from alembic import op
from app.core.config import settings

revision = "20260916_19"
down_revision = "20260916_18"
branch_labels = None
depends_on = None


def _role(name: str) -> str:
    return op.get_bind().dialect.identifier_preparer.quote(name)


def _link_work_scope() -> str:
    return (
        "EXISTS (SELECT 1 FROM issues issue "
        "WHERE issue.id = issue_retest_links.issue_id "
        "AND issue.workspace_id = public.work_management_workspace_id() "
        "AND issue.work_id = public.work_management_id())"
    )


def upgrade() -> None:
    app_role = _role(settings.db_user)
    op.add_column(
        "issues",
        sa.Column(
            "adjustment_generation",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "issue_retest_links",
        sa.Column(
            "adjustment_generation",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint(
        "ck_issue_adjustment_generation_nonnegative",
        "issues",
        "adjustment_generation >= 0",
    )
    op.create_check_constraint(
        "ck_issue_retest_link_adjustment_generation_nonnegative",
        "issue_retest_links",
        "adjustment_generation >= 0",
    )
    op.execute(
        "UPDATE issues SET adjustment_generation = 1 WHERE adjustment_note IS NOT NULL"
    )
    op.execute(
        "UPDATE issue_retest_links link "
        "SET adjustment_generation = issue.adjustment_generation "
        "FROM issues issue WHERE issue.id = link.issue_id"
    )
    op.alter_column(
        "issues",
        "adjustment_generation",
        existing_type=sa.Integer(),
        server_default=None,
    )
    op.alter_column(
        "issue_retest_links",
        "adjustment_generation",
        existing_type=sa.Integer(),
        server_default=None,
    )
    op.execute(
        f"GRANT UPDATE (adjustment_note, adjustment_generation) ON TABLE issues TO {app_role}"
    )
    for policy in (
        "issue_retest_link_update",
        "issue_retest_link_insert",
        "issue_retest_link_read",
    ):
        op.execute(f"DROP POLICY {policy} ON issue_retest_links")
    op.execute("DROP FUNCTION public.issue_retest_issue_id()")
    link_work_scope = _link_work_scope()
    op.execute(
        "CREATE POLICY issue_retest_link_read ON issue_retest_links FOR SELECT "
        f"USING ({link_work_scope})"
    )
    op.execute(
        "CREATE POLICY issue_retest_link_insert ON issue_retest_links FOR INSERT "
        f"WITH CHECK ({link_work_scope})"
    )
    op.execute(
        "CREATE POLICY issue_retest_link_update ON issue_retest_links FOR UPDATE "
        f"USING ({link_work_scope}) WITH CHECK ({link_work_scope})"
    )


def downgrade() -> None:
    app_role = _role(settings.db_user)
    for policy in (
        "issue_retest_link_update",
        "issue_retest_link_insert",
        "issue_retest_link_read",
    ):
        op.execute(f"DROP POLICY {policy} ON issue_retest_links")
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
    op.execute(f"REVOKE UPDATE (adjustment_generation) ON TABLE issues FROM {app_role}")
    op.drop_constraint(
        "ck_issue_retest_link_adjustment_generation_nonnegative",
        "issue_retest_links",
        type_="check",
    )
    op.drop_constraint(
        "ck_issue_adjustment_generation_nonnegative",
        "issues",
        type_="check",
    )
    op.drop_column("issue_retest_links", "adjustment_generation")
    op.drop_column("issues", "adjustment_generation")
