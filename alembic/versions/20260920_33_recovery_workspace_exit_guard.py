"""允许恢复 scope 读取已过期退出工作空间。"""

from alembic import op

revision = "20260920_33"
down_revision = "20260920_32"
branch_labels = None
depends_on = None


def _schema_check(expected_revision: str) -> None:
    op.execute(
        "CREATE OR REPLACE FUNCTION public.recovery_schema_current() RETURNS boolean "
        "LANGUAGE sql STABLE SECURITY DEFINER "
        "SET search_path = pg_catalog, public AS $$ "
        "SELECT EXISTS (SELECT 1 FROM public.alembic_version "
        f"WHERE version_num = '{expected_revision}') $$"
    )


def _workspace_exit_read_guard(include_recovery: bool) -> None:
    recovery = " OR public.recovery_maintenance()" if include_recovery else ""
    op.execute("DROP POLICY workspace_exit_read ON workspaces")
    op.execute(
        "CREATE POLICY workspace_exit_read ON workspaces AS RESTRICTIVE "
        "FOR SELECT USING ("
        "exit_read_until IS NULL OR exit_read_until > CURRENT_TIMESTAMP "
        "OR public.workspace_exit_processor() = 'active'"
        f"{recovery})"
    )


def upgrade() -> None:
    _workspace_exit_read_guard(include_recovery=True)
    _schema_check(revision)


def downgrade() -> None:
    _workspace_exit_read_guard(include_recovery=False)
    _schema_check("20260920_32")
