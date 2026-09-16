"""增加实际玩法模式与试玩概览只读范围。"""

import sqlalchemy as sa

from alembic import op
from app.core.config import settings

revision = "20260916_22"
down_revision = "20260916_21"
branch_labels = None
depends_on = None


def _app_role() -> str:
    return op.get_bind().dialect.identifier_preparer.quote(settings.db_user)


def upgrade() -> None:
    app_role = _app_role()
    op.add_column(
        "playtest_sessions", sa.Column("actual_play_mode", sa.String(length=160))
    )

    op.execute(
        """
        CREATE FUNCTION public.playtest_overview_workspace_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(
                current_setting('app.playtest_overview_workspace_id', true), ''
            )::uuid
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.playtest_overview_work_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(
                current_setting('app.playtest_overview_work_id', true), ''
            )::uuid
        $$
        """
    )
    for function in (
        "playtest_overview_workspace_id",
        "playtest_overview_work_id",
    ):
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{function}() TO {app_role}")

    overview_scope = (
        "workspace_id = public.playtest_overview_workspace_id() "
        "AND work_id = public.playtest_overview_work_id()"
    )
    op.execute(
        "CREATE POLICY playtest_session_overview_read ON playtest_sessions FOR SELECT "
        f"USING ({overview_scope})"
    )
    op.execute(
        """
        CREATE POLICY playtest_observation_overview_read
        ON playtest_observations FOR SELECT USING (
            EXISTS (
                SELECT 1 FROM playtest_sessions session
                WHERE session.id = playtest_observations.session_id
                  AND session.workspace_id = public.playtest_overview_workspace_id()
                  AND session.work_id = public.playtest_overview_work_id()
            )
        )
        """
    )
    op.execute(
        "CREATE POLICY issue_overview_read ON issues FOR SELECT "
        f"USING ({overview_scope})"
    )
    op.execute(
        """
        CREATE POLICY issue_retest_link_overview_read
        ON issue_retest_links FOR SELECT USING (
            EXISTS (
                SELECT 1 FROM issues issue
                WHERE issue.id = issue_retest_links.issue_id
                  AND issue.workspace_id = public.playtest_overview_workspace_id()
                  AND issue.work_id = public.playtest_overview_work_id()
            )
        )
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS issue_retest_link_overview_read ON issue_retest_links"
    )
    op.execute("DROP POLICY IF EXISTS issue_overview_read ON issues")
    op.execute(
        "DROP POLICY IF EXISTS playtest_observation_overview_read "
        "ON playtest_observations"
    )
    op.execute(
        "DROP POLICY IF EXISTS playtest_session_overview_read ON playtest_sessions"
    )
    op.execute("DROP FUNCTION public.playtest_overview_work_id()")
    op.execute("DROP FUNCTION public.playtest_overview_workspace_id()")
    op.drop_column("playtest_sessions", "actual_play_mode")
