"""使恢复 scope 通过退出读取守卫并更新 schema 核验。"""

from alembic import op

revision = "20260920_32"
down_revision = "20260920_31"
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
    op.execute(
        "CREATE OR REPLACE FUNCTION public.workspace_exit_allows_read(p_workspace_id uuid) "
        "RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER "
        "SET search_path = pg_catalog, public AS $$ "
        "SELECT EXISTS ("
        "SELECT 1 FROM public.workspaces workspace "
        "WHERE workspace.id = p_workspace_id AND ("
        "workspace.exit_read_until IS NULL "
        "OR workspace.exit_read_until > CURRENT_TIMESTAMP "
        "OR (workspace.id = public.workspace_exit_maintenance_id() "
        "AND public.workspace_exit_processor() = 'active')"
        f"{recovery}"
        ")) $$"
    )


def upgrade() -> None:
    op.drop_constraint(
        "ck_recovery_state_authorization_mode", "recovery_state", type_="check"
    )
    op.create_check_constraint(
        "ck_recovery_state_authorization_mode",
        "recovery_state",
        "authorization_mode IS NULL OR authorization_mode = 'restricted'",
    )
    _workspace_exit_read_guard(include_recovery=True)
    _schema_check(revision)


def downgrade() -> None:
    op.drop_constraint(
        "ck_recovery_state_authorization_mode", "recovery_state", type_="check"
    )
    op.create_check_constraint(
        "ck_recovery_state_authorization_mode",
        "recovery_state",
        "authorization_mode IS NULL OR authorization_mode IN ('fresh', 'restricted')",
    )
    _workspace_exit_read_guard(include_recovery=False)
    _schema_check("20260920_31")
